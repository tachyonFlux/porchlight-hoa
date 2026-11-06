"""Bank file parsing (pure functions; no database access).

Supports CSV exports (auto-detected or user-mapped columns) and OFX/QFX statements.
Each parsed row gets a stable dedupe key so overlapping downloads can be imported
repeatedly without creating duplicates.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.utils import timezone

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 20000

DATE_FORMATS = ["%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y", "%d-%b-%Y", "%b %d, %Y", "%Y/%m/%d"]

# Header names seen in common bank exports, lower-cased. Order matters (first match wins).
HEADER_HINTS = {
    "date": ["posting date", "posted date", "transaction date", "date", "trans date"],
    "description": ["transaction description", "description", "payee", "details", "name", "memo"],
    "amount": ["amount", "transaction amount"],
    "withdrawal": ["withdrawals", "withdrawal", "debit", "debits", "money out"],
    "deposit": ["deposits", "deposit", "credit", "credits", "money in"],
    "check_number": ["check number", "check #", "check no", "check", "reference number", "reference"],
    "memo": ["memo", "notes", "note"],
}


class ParseError(Exception):
    pass


@dataclass
class ParsedRow:
    line: int
    date: date | None = None
    description: str = ""
    amount_cents: int | None = None
    check_number: str = ""
    memo: str = ""
    category: str = ""
    fitid: str = ""
    dedupe_key: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self):
        return not self.errors


def decode(raw):
    if len(raw) > MAX_FILE_BYTES:
        raise ParseError("That file is too large (5 MB max).")
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ParseError("Couldn't read that file's text encoding.")


def parse_amount(value):
    """'$1,234.56', '(12.00)', '-12', '12.00-', '' -> cents or None."""
    text = (value or "").strip().replace("$", "").replace(",", "").replace(" ", "")
    if not text:
        return None
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    if text.endswith("-"):
        negative, text = True, text[:-1]
    if text.startswith("-"):
        negative, text = not negative, text[1:]
    elif text.startswith("+"):
        text = text[1:]
    if "." in text and len(text.rsplit(".", 1)[1]) > 2:
        # e.g. European "1.234,56" or a typo; better to stop than to import the wrong amount.
        raise ValueError(f"'{value}' has more than two decimal places")
    try:
        cents = int((Decimal(text) * 100).quantize(Decimal("1")))
    except InvalidOperation as exc:
        raise ValueError(f"'{value}' isn't an amount") from exc
    return -cents if negative else cents


def parse_date(value, fmt=None):
    text = (value or "").strip()
    if not text:
        raise ValueError("missing date")
    formats = [fmt] if fmt else DATE_FORMATS
    for f in formats:
        try:
            return datetime.strptime(text, f).date()
        except ValueError:
            continue
    raise ValueError(f"'{value}' isn't a date we recognize")


def normalize_description(text):
    return re.sub(r"\s+", " ", (text or "").strip().upper())


def assign_dedupe_keys(rows):
    """CSV rows have no bank id, so key on date + amount + description, numbering exact
    repeats within the file (two identical $25 checks the same day stay two rows, and the
    same two rows in an overlapping download map to the same keys)."""
    seen = {}
    for row in rows:
        if not row.ok:
            continue
        base = f"{row.date.isoformat()}|{row.amount_cents}|{normalize_description(row.description)}"
        n = seen.get(base, 0)
        seen[base] = n + 1
        row.dedupe_key = "csv:" + hashlib.sha256(f"{base}|{n}".encode()).hexdigest()[:40]


def read_csv(text):
    """Return (headers, records). Handles files with a few preamble lines before the header."""
    lines = text.splitlines()
    sample = "\n".join(lines[:50])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    # Header = first row that looks like one (has a date-ish column name).
    for i, row in enumerate(rows[:20]):
        lowered = [c.strip().lower() for c in row]
        if any(h in lowered for h in HEADER_HINTS["date"]):
            headers = [c.strip() for c in row]
            records = [r for r in rows[i + 1 :] if any(c.strip() for c in r)]
            if len(records) > MAX_ROWS:
                raise ParseError(f"Too many rows (max {MAX_ROWS}).")
            return headers, records
    raise ParseError("Couldn't find a header row with a date column. Choose the columns by hand.")


def guess_mapping(headers):
    lowered = [h.lower() for h in headers]
    mapping = {}
    used = set()
    for target, hints in HEADER_HINTS.items():
        for hint in hints:
            if hint in lowered and lowered.index(hint) not in used:
                idx = lowered.index(hint)
                mapping[target] = headers[idx]
                used.add(idx)
                break
    return mapping


def apply_mapping(headers, records, mapping, date_format=None, flip_sign=False):
    """Turn CSV records into ParsedRows using a column mapping."""
    index = {name: i for i, name in enumerate(headers)}
    if "date" not in mapping or "description" not in mapping:
        raise ParseError("Choose at least the date and description columns.")
    if "amount" not in mapping and not ("withdrawal" in mapping or "deposit" in mapping):
        raise ParseError("Choose an amount column, or withdrawal/deposit columns.")
    for col in mapping.values():
        if col not in index:
            raise ParseError(f"Column '{col}' isn't in this file.")

    def cell(record, key):
        col = mapping.get(key)
        if not col:
            return ""
        i = index[col]
        return record[i].strip() if i < len(record) else ""

    rows = []
    for n, record in enumerate(records, start=2):
        row = ParsedRow(
            line=n,
            description=cell(record, "description")[:300],
            memo=cell(record, "memo")[:300],
            check_number=cell(record, "check_number")[:20],
        )
        try:
            row.date = parse_date(cell(record, "date"), date_format)
        except ValueError as exc:
            row.errors.append(str(exc))
        try:
            if "amount" in mapping:
                amount = parse_amount(cell(record, "amount"))
            else:
                out = parse_amount(cell(record, "withdrawal"))
                into = parse_amount(cell(record, "deposit"))
                amount = None if out is None and into is None else (into or 0) - abs(out or 0)
            if amount is None:
                row.errors.append("missing amount")
            else:
                row.amount_cents = -amount if flip_sign else amount
        except ValueError as exc:
            row.errors.append(str(exc))
        if not row.description:
            row.errors.append("missing description")
        rows.append(row)
    assign_dedupe_keys(rows)
    return rows


def parse_ofx(raw, want_last4=""):
    """Parse OFX/QFX (v1 SGML or v2 XML). Returns (account_id_last4, rows).

    A file with several accounts uses the one ending in `want_last4` (else the first)."""
    from ofxtools.Parser import OFXTree

    if len(raw) > MAX_FILE_BYTES:
        raise ParseError("That file is too large (5 MB max).")
    tree = OFXTree()
    try:
        tree.parse(io.BytesIO(raw))
        ofx = tree.convert()
    except Exception as exc:
        raise ParseError(f"Couldn't read that OFX/QFX file ({exc.__class__.__name__}).") from exc
    statements = list(getattr(ofx, "statements", []) or [])
    if not statements:
        raise ParseError("That file doesn't contain a bank statement.")

    def acct_of(st):
        return str(getattr(getattr(st, "account", None), "acctid", "") or "")

    stmt = next((st for st in statements if want_last4 and acct_of(st).endswith(want_last4)), statements[0])
    acct_id = acct_of(stmt)
    txns = list(stmt.banktranlist or [])
    if len(txns) > MAX_ROWS:
        raise ParseError(f"Too many transactions (max {MAX_ROWS}).")
    rows = []
    for n, txn in enumerate(txns, start=1):
        posted = txn.dtposted
        if isinstance(posted, datetime):
            # OFX times are usually UTC; use the HOA's local date so a 7pm Dec 31 item stays in December.
            posted = timezone.localtime(posted).date() if timezone.is_aware(posted) else posted.date()
        description = " ".join(p for p in (txn.name or "", txn.memo or "") if p).strip() or (
            txn.trntype or ""
        )
        row = ParsedRow(
            line=n,
            date=posted,
            description=description[:300],
            amount_cents=int((Decimal(txn.trnamt) * 100).quantize(Decimal("1"))),
            check_number=str(txn.checknum or "")[:20],
            memo=(txn.memo or "")[:300],
            fitid=str(txn.fitid or "")[:100],
        )
        if row.fitid:
            row.dedupe_key = "ofx:" + hashlib.sha256(row.fitid.encode()).hexdigest()[:40]
        rows.append(row)
    no_fitid = [r for r in rows if not r.dedupe_key]
    assign_dedupe_keys(no_fitid)
    return acct_id[-4:], rows
