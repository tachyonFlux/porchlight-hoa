"""Optional read-only bank sync through SimpleFIN (https://www.simplefin.org/protocol.html).

Porchlight never sees bank usernames or passwords. The board pastes a one-time *setup
token* made on the SimpleFIN Bridge site; we exchange it for an *access URL* (which has
credentials embedded) and store that encrypted with PORCHLIGHT_ENCRYPTION_KEY.
"""

import base64
import hashlib
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.utils import timezone

from .parsers import ParsedRow

TIMEOUT = 30
MAX_RESPONSE_BYTES = 20 * 1024 * 1024


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
    something on the local network."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise SimpleFINError("SimpleFIN URLs must use https.")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SimpleFINError(f"Couldn't find {parts.hostname}.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise SimpleFINError("That URL points to a private network address.")
    return parts


def _request(url, method="GET", data=None):
    parts = _check_public_https(url)
    headers = {"User-Agent": "Porchlight", "Accept": "application/json"}
    if parts.username:
        creds = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password or '')}"
        headers["Authorization"] = "Basic " + base64.b64encode(creds.encode()).decode()
        netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
        url = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
    req = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310 - https checked
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310
            body = resp.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise SimpleFINError(
                "SimpleFIN refused access (the token was already used or access was revoked)."
            ) from exc
        if exc.code == 402:
            raise SimpleFINError("SimpleFIN says payment is required on your Bridge account.") from exc
        raise SimpleFINError(f"SimpleFIN returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SimpleFINError(f"Couldn't reach SimpleFIN ({exc}).") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise SimpleFINError("SimpleFIN's response was unexpectedly large.")
    return body


def claim(setup_token):
    """Exchange a one-time setup token for the access URL."""
    try:
        claim_url = base64.b64decode(setup_token.strip(), validate=True).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise SimpleFINError("That doesn't look like a SimpleFIN setup token.") from exc
    access_url = _request(claim_url, method="POST", data=b"").decode().strip()
    if not access_url.startswith("https://"):
        raise SimpleFINError("SimpleFIN returned an unexpected response.")
    return access_url


def fetch_accounts(access_url, since):
    query = urllib.parse.urlencode(
        {"start-date": int(datetime.combine(since, datetime.min.time(), UTC).timestamp())}
    )
    body = _request(f"{access_url.rstrip('/')}/accounts?{query}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SimpleFINError("SimpleFIN returned something that isn't JSON.") from exc
    errors = [e.get("msg", str(e)) if isinstance(e, dict) else str(e) for e in data.get("errlist", [])]
    errors += [str(e) for e in data.get("errors", [])]
    return data.get("accounts", []), errors


def _cents(value):
    return int((Decimal(str(value)) * 100).quantize(Decimal("1")))


def account_rows(account):
    """SimpleFIN transactions -> ParsedRows (pending ones are skipped)."""
    rows = []
    for n, txn in enumerate(account.get("transactions", []) or [], start=1):
        if txn.get("pending") or not txn.get("posted"):
            continue
        posted = datetime.fromtimestamp(int(txn["posted"]), tz=UTC)
        row = ParsedRow(
            line=n,
            date=timezone.localtime(posted).date(),
            description=(txn.get("description") or txn.get("payee") or "")[:300] or "(no description)",
            amount_cents=_cents(txn["amount"]),
            fitid=str(txn.get("id", ""))[:100],
        )
        row.dedupe_key = (
            "sfin:" + hashlib.sha256(f"{account.get('id')}|{row.fitid}".encode()).hexdigest()[:40]
        )
        rows.append(row)
    return rows


def default_since(last_sync_at):
    """Overlap a week with the last sync (late-posting items), or 60 days on first sync."""
    today = timezone.localdate()
    if last_sync_at:
        return min(today, timezone.localtime(last_sync_at).date()) - timedelta(days=7)
    return today - timedelta(days=60)
