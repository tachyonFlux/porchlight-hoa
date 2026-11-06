"""Optional read-only bank sync through SimpleFIN (https://www.simplefin.org/protocol.html).

Porchlight never sees bank usernames or passwords. The board pastes a one-time *setup
token* made on the SimpleFIN Bridge site; we exchange it for an *access URL* (which has
credentials embedded) and store that encrypted with PORCHLIGHT_ENCRYPTION_KEY.
"""

import base64
import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import urllib.parse
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .parsers import MAX_ROWS, ParsedRow

TIMEOUT = 30
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
FIRST_SYNC_DAYS = 60


class SimpleFINError(Exception):
    pass


def is_enabled():
    return bool(settings.SIMPLEFIN_ENABLED and settings.ENCRYPTION_KEY)


def _fernet():
    if not settings.ENCRYPTION_KEY:
        raise SimpleFINError("PORCHLIGHT_ENCRYPTION_KEY isn't set on the server.")
    try:
        return Fernet(settings.ENCRYPTION_KEY.encode())
    except ValueError as exc:
        raise SimpleFINError("PORCHLIGHT_ENCRYPTION_KEY isn't a valid key (see .env.example).") from exc


def encrypt(text):
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token):
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SimpleFINError(
            "Couldn't decrypt the saved connection; was the encryption key changed?"
        ) from exc


def _check_public_https(url):
    """Only talk to public HTTPS hosts, so a crafted token can't make the server call
    something on the local network. Returns (parts, ip) so the connection goes to the
    address that was checked (no second DNS lookup an attacker could change)."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port or 443
    except ValueError as exc:
        raise SimpleFINError("That isn't a valid SimpleFIN URL.") from exc
    if parts.scheme != "https" or not parts.hostname:
        raise SimpleFINError("SimpleFIN URLs must use https.")
    try:
        infos = socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        raise SimpleFINError(f"Couldn't find {parts.hostname}.") from exc
    ips = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not ips or any(not ip.is_global for ip in ips):
        raise SimpleFINError("That URL points to a private network address.")
    return parts, str(ips[0])


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a pre-checked IP, still verifying the certificate for the real hostname."""

    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = ip

    def connect(self):
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


@sensitive_variables("url", "creds", "headers")
def _request(url, method="GET", data=None):
    parts, ip = _check_public_https(url)
    headers = {"User-Agent": "Porchlight", "Accept": "application/json"}
    if parts.username:
        creds = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password or '')}"
        headers["Authorization"] = "Basic " + base64.b64encode(creds.encode()).decode()
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    conn = _PinnedHTTPSConnection(parts.hostname, parts.port or 443, ip, TIMEOUT)
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        body = resp.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise SimpleFINError(f"Couldn't reach SimpleFIN ({exc.__class__.__name__}).") from None
    finally:
        conn.close()
    # Redirects are never followed: they could point at a private address, and the
    # Authorization header must only go to the host it was issued for.
    if 300 <= status < 400:
        raise SimpleFINError("SimpleFIN tried to redirect the request; refusing.")
    if status == 403:
        raise SimpleFINError("SimpleFIN refused access (the token was already used or access was revoked).")
    if status == 402:
        raise SimpleFINError("SimpleFIN says payment is required on your Bridge account.")
    if status >= 400:
        raise SimpleFINError(f"SimpleFIN returned HTTP {status}.")
    if len(body) > MAX_RESPONSE_BYTES:
        raise SimpleFINError("SimpleFIN's response was unexpectedly large.")
    return body


@sensitive_variables("setup_token", "claim_url", "access_url")
def claim(setup_token):
    """Exchange a one-time setup token for the access URL."""
    try:
        claim_url = base64.b64decode(setup_token.strip(), validate=True).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise SimpleFINError("That doesn't look like a SimpleFIN setup token.") from exc
    access_url = _request(claim_url, method="POST", data=b"").decode("utf-8", "replace").strip()
    if not access_url.startswith("https://") or len(access_url) > 2000:
        raise SimpleFINError("SimpleFIN returned an unexpected response.")
    return access_url


@sensitive_variables("access_url")
def fetch_accounts(access_url, since):
    query = urllib.parse.urlencode(
        {"start-date": int(datetime.combine(since, datetime.min.time(), UTC).timestamp())}
    )
    body = _request(f"{access_url.rstrip('/')}/accounts?{query}")
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SimpleFINError("SimpleFIN returned something that isn't JSON.") from exc
    if not isinstance(data, dict):
        raise SimpleFINError("SimpleFIN returned an unexpected response.")
    errors = [e.get("msg", str(e)) if isinstance(e, dict) else str(e) for e in _as_list(data.get("errlist"))]
    errors += [str(e) for e in _as_list(data.get("errors"))]
    accounts = [a for a in _as_list(data.get("accounts")) if isinstance(a, dict) and a.get("id")]
    return accounts, [e[:300] for e in errors]


def _as_list(value):
    return value if isinstance(value, list) else []


def _cents(value):
    return int((Decimal(str(value)) * 100).quantize(Decimal("1")))


def account_rows(account):
    """SimpleFIN transactions -> ParsedRows (pending and malformed ones are skipped)."""
    rows = []
    for n, txn in enumerate(_as_list(account.get("transactions")), start=1):
        if not isinstance(txn, dict) or txn.get("pending") or not txn.get("posted"):
            continue
        try:
            posted = datetime.fromtimestamp(int(txn["posted"]), tz=UTC)
            amount = _cents(txn["amount"])
        except (KeyError, TypeError, ValueError, OverflowError, ArithmeticError):
            continue
        row = ParsedRow(
            line=n,
            date=timezone.localtime(posted).date(),
            description=str(txn.get("description") or txn.get("payee") or "")[:300] or "(no description)",
            amount_cents=amount,
            fitid=str(txn.get("id", ""))[:100],
        )
        row.dedupe_key = (
            "sfin:" + hashlib.sha256(f"{account.get('id')}|{row.fitid}".encode()).hexdigest()[:40]
        )
        rows.append(row)
        if len(rows) >= MAX_ROWS:
            break
    return rows


def default_since(last_sync_at):
    """Overlap a week with the last sync (late-posting items), or 60 days on first sync."""
    today = timezone.localdate()
    if last_sync_at:
        return min(today, timezone.localtime(last_sync_at).date()) - timedelta(days=7)
    return today - timedelta(days=FIRST_SYNC_DAYS)
