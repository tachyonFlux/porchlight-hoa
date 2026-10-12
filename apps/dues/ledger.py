"""Pure ledger math: running balances and oldest-first allocation of credits to charges."""

from dataclasses import dataclass
from datetime import date


@dataclass
class Line:
    id: int
    date: date
    amount_cents: int  # + charge, - credit
    invoice_id: int | None = None
    kind: str = ""


def running_balance(lines):
    """Yield (line, balance_after) in date order."""
    balance = 0
    for line in sorted(lines, key=lambda x: (x.date, x.id)):
        balance += line.amount_cents
        yield line, balance


DUES_KINDS = ("invoice",)


def allocate(lines, as_of=None):
    """Apply credits to charges. Returns {charge_line_id: unpaid_cents}.

    Only lines dated on or before `as_of` count (all lines if None).

    Order: dues invoices first (oldest first), then everything else (late fees, manual
    charges such as opening balances or fines), oldest first. Paying dues before fees means
    an on-time dues payment is never treated as late because of an older, unrelated charge.
    The total balance is the same either way; only which charge counts as "open" differs.

    A late-fee waiver cancels its own invoice's fee. A voided payment's reversal (a
    positive "payment_void" line) cancels the credit rather than acting as a charge.
    """
    relevant = [x for x in lines if as_of is None or x.date <= as_of]
    charges = sorted(
        (x for x in relevant if x.amount_cents > 0 and x.kind != "payment_void"),
        key=lambda x: (x.kind not in DUES_KINDS, x.date, x.id),
    )
    remaining = {c.id: c.amount_cents for c in charges}
    late_fee_by_invoice = {c.invoice_id: c.id for c in charges if c.kind == "late_fee"}
    pool = 0
    for x in relevant:
        if x.kind == "late_fee_waiver" and x.invoice_id in late_fee_by_invoice:
            fee_id = late_fee_by_invoice[x.invoice_id]
            take = min(-x.amount_cents, remaining[fee_id])
            remaining[fee_id] -= take
            pool += -x.amount_cents - take
        elif x.amount_cents < 0 or x.kind == "payment_void":
            pool -= x.amount_cents  # credits add; a voided payment's reversal removes
    unpaid = {}
    for charge in charges:
        applied = min(max(pool, 0), remaining[charge.id])
        pool -= applied
        unpaid[charge.id] = remaining[charge.id] - applied
    return unpaid


def invoice_unpaid(lines, invoice_id, as_of=None):
    """Unpaid cents of the invoice's own charge (excluding its late fee)."""
    unpaid = allocate(lines, as_of)
    for line in lines:
        if line.invoice_id == invoice_id and line.kind == "invoice":
            return unpaid.get(line.id, 0)
    return 0


def invoice_outstanding(lines, invoice_id, as_of=None):
    """Unpaid cents of the invoice charge plus any late fee on it, net of waivers."""
    unpaid = allocate(lines, as_of)
    total = 0
    for line in lines:
        if line.invoice_id != invoice_id or (as_of is not None and line.date > as_of):
            continue
        if line.kind in ("invoice", "late_fee"):
            total += unpaid.get(line.id, 0)
    return total
