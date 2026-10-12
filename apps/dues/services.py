"""Dues service layer. Every write to invoices, payments, and the ledger goes through here.

Scheduled functions (generate_invoices, apply_late_fees, send_reminders) are idempotent:
database unique constraints guarantee that re-running them never double-bills.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from apps.community.models import Home, Residency
from apps.core import audit
from apps.core.email import send_templated
from apps.core.money import format_money, percent_of
from apps.core.utils import absolute_url

from . import ledger, periods
from .models import BillingPeriod, DuesSettings, Invoice, LedgerEntry, Payment, ReminderLog

log = logging.getLogger("porchlight.dues")
K = LedgerEntry.Kind


# ------------------------------------------------------------------ reading


def _lines(home):
    return [
        ledger.Line(e.id, e.date, e.amount_cents, e.invoice_id, e.kind)
        for e in LedgerEntry.objects.filter(home=home)
    ]


def balance(home, as_of=None):
    qs = LedgerEntry.objects.filter(home=home)
    if as_of:
        qs = qs.filter(date__lte=as_of)
    return qs.aggregate(total=Sum("amount_cents"))["total"] or 0


def balances_by_home(as_of=None):
    qs = LedgerEntry.objects.all()
    if as_of:
        qs = qs.filter(date__lte=as_of)
    return {
        row["home_id"]: row["total"] or 0 for row in qs.values("home_id").annotate(total=Sum("amount_cents"))
    }


def ledger_rows(home):
    """Entries with running balance, newest first for display."""
    entries = {
        e.id: e for e in LedgerEntry.objects.filter(home=home).select_related("payment", "invoice__period")
    }
    rows = [(entries[line.id], bal) for line, bal in ledger.running_balance(_lines(home))]
    rows.reverse()
    return rows


@dataclass
class InvoiceStatus:
    invoice: Invoice
    unpaid_cents: int  # invoice charge + late fee still owed
    late_fee_cents: int = 0
    state: str = "open"  # paid / open / overdue
    paid_cents: int = field(init=False)

    def __post_init__(self):
        self.paid_cents = max(self.invoice.amount_cents + self.late_fee_cents - self.unpaid_cents, 0)


def invoice_status(invoice, today=None, lines=None):
    today = today or timezone.localdate()
    lines = lines if lines is not None else _lines(invoice.home)
    unpaid = ledger.invoice_outstanding(lines, invoice.id)
    fee = sum(
        x.amount_cents
        for x in lines
        if x.invoice_id == invoice.id and x.kind in (K.LATE_FEE, K.LATE_FEE_WAIVER)
    )
    if unpaid <= 0:
        state = "paid"
    elif today > invoice.period.late_after:
        state = "overdue"
    else:
        state = "open"
    return InvoiceStatus(invoice, max(unpaid, 0), max(fee, 0), state)


def statuses_for_period(period, today=None):
    invoices = list(period.invoices.select_related("home", "period"))
    lines_by_home = {}
    for e in LedgerEntry.objects.filter(home__in=[i.home_id for i in invoices]):
        lines_by_home.setdefault(e.home_id, []).append(
            ledger.Line(e.id, e.date, e.amount_cents, e.invoice_id, e.kind)
        )
    return [invoice_status(inv, today, lines_by_home.get(inv.home_id, [])) for inv in invoices]


def household_emails(home):
    return sorted(
        {
            r.user.email
            for r in Residency.objects.current().filter(home=home).select_related("user")
            if r.user.email
        }
    )


# ------------------------------------------------------------------ board actions


@transaction.atomic
def record_payment(
    home, amount_cents, method, received_on, check_number="", note="", actor=None, request=None
):
    if amount_cents <= 0:
        raise ValueError("Payment must be positive.")
    payment = Payment.objects.create(
        home=home,
        amount_cents=amount_cents,
        method=method,
        received_on=received_on,
        check_number=check_number.strip(),
        note=note,
        recorded_by=actor,
    )
    LedgerEntry.objects.create(
        home=home,
        date=received_on,
        kind=K.PAYMENT,
        amount_cents=-amount_cents,
        payment=payment,
        memo=str(payment),
        created_by=actor,
    )
    audit.record(
        request or actor,
        "dues.payment_recorded",
        payment,
        {"home": str(home), "amount": format_money(amount_cents), "method": method, "check": check_number},
    )
    return payment


@transaction.atomic
def void_payment(payment, reason, actor=None, request=None):
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.voided_at:
        return payment
    payment.voided_at = timezone.now()
    payment.void_reason = reason
    payment.save(update_fields=["voided_at", "void_reason"])
    LedgerEntry.objects.create(
        home=payment.home,
        date=timezone.localdate(),
        kind=K.PAYMENT_VOID,
        amount_cents=payment.amount_cents,
        payment=payment,
        memo=f"Voided: {reason}"[:200],
        created_by=actor,
    )
    audit.record(
        request or actor,
        "dues.payment_voided",
        payment,
        {"amount": format_money(payment.amount_cents), "reason": reason},
    )
    return payment


@transaction.atomic
def add_adjustment(home, amount_cents, memo, entry_date, actor=None, request=None):
    """Positive = extra charge (e.g. opening balance), negative = credit."""
    if amount_cents == 0:
        raise ValueError("Amount can't be zero.")
    entry = LedgerEntry.objects.create(
        home=home,
        date=entry_date,
        kind=K.CHARGE if amount_cents > 0 else K.CREDIT,
        amount_cents=amount_cents,
        memo=memo,
        created_by=actor,
    )
    audit.record(
        request or actor,
        "dues.adjustment",
        home,
        {"amount": format_money(amount_cents, signed=True), "memo": memo},
    )
    return entry


@transaction.atomic
def waive_late_fee(invoice, actor=None, request=None, reason=""):
    fee = LedgerEntry.objects.filter(invoice=invoice, kind=K.LATE_FEE).first()
    if fee is None:
        raise ValueError("This invoice has no late fee.")
    try:
        with transaction.atomic():
            entry = LedgerEntry.objects.create(
                home=invoice.home,
                date=timezone.localdate(),
                kind=K.LATE_FEE_WAIVER,
                amount_cents=-fee.amount_cents,
                invoice=invoice,
                memo=f"Late fee waived{': ' + reason if reason else ''}"[:200],
                created_by=actor,
            )
    except IntegrityError:
        return None  # already waived
    audit.record(
        request or actor,
        "dues.late_fee_waived",
        invoice,
        {"amount": format_money(fee.amount_cents), "reason": reason},
    )
    return entry


def ensure_period(spec, settings):
    period, _ = BillingPeriod.objects.get_or_create(
        start=spec.start,
        defaults={
            "end": spec.end,
            "due_date": spec.due_date,
            "label": periods.period_label(spec, settings.frequency),
            "amount_cents": settings.amount_cents,
            "grace_days": settings.grace_days,
        },
    )
    return period


def create_invoice(home, period, issued_on, actor=None):
    """Create the invoice and its ledger charge. Returns None if it already exists."""
    try:
        with transaction.atomic():
            invoice = Invoice.objects.create(
                home=home, period=period, amount_cents=period.amount_cents, issued_on=issued_on
            )
            LedgerEntry.objects.create(
                home=home,
                date=issued_on,
                kind=K.INVOICE,
                amount_cents=invoice.amount_cents,
                invoice=invoice,
                memo=period.label,
                created_by=actor,
            )
    except IntegrityError:
        return None
    return invoice


def bill_homes(period, homes, issued_on=None, actor=None, request=None):
    """Board action: invoice the given homes for a period (skips ones already billed)."""
    issued_on = issued_on or timezone.localdate()
    created = [inv for home in homes if (inv := create_invoice(home, period, issued_on, actor))]
    if created:
        audit.record(request or actor, "dues.invoices_created", period, {"count": len(created)})
    return created


# ------------------------------------------------------------------ scheduled jobs


def generate_invoices(today):
    """Create periods and invoices whose issue date has arrived. Idempotent."""
    settings = DuesSettings.load()
    if not settings.auto_invoicing or settings.amount_cents <= 0:
        return {"invoices_created": 0}
    specs = periods.periods_to_issue(
        today,
        frequency=settings.frequency,
        anchor_month=settings.anchor_month,
        due_day=settings.due_day,
        lead_days=settings.invoice_lead_days,
        billing_start=settings.billing_start_date,
    )
    created = []
    for spec in specs:
        period = ensure_period(spec, settings)
        issued_on = spec.issue_date(settings.invoice_lead_days)
        # Homes added after a period's invoices went out aren't billed retroactively;
        # the board can bill them by hand from the period page.
        homes = Home.objects.filter(is_active=True, created_at__date__lte=max(issued_on, today))
        if Invoice.objects.filter(period=period).exists():
            homes = homes.filter(created_at__date__lte=issued_on)
        for home in homes.exclude(invoices__period=period):
            invoice = create_invoice(home, period, issued_on)
            if invoice:
                created.append(invoice)
    if settings.email_new_invoices:
        for invoice in created:
            _send_reminder(invoice, ReminderLog.Kind.ISSUED, 0, today)
    return {"invoices_created": len(created)}


def late_fee_amount(settings, unpaid_cents):
    if settings.late_fee_type == DuesSettings.LateFeeType.FLAT:
        return settings.late_fee_flat_cents
    if settings.late_fee_type == DuesSettings.LateFeeType.PERCENT:
        return percent_of(unpaid_cents, settings.late_fee_percent)
    return 0


def apply_late_fees(today):
    """Charge one late fee per invoice still unpaid at the end of its grace period."""
    settings = DuesSettings.load()
    if settings.late_fee_type == DuesSettings.LateFeeType.NONE:
        return {"late_fees_applied": 0}
    applied = 0
    candidates = (
        Invoice.objects.select_related("period", "home")
        .exclude(entries__kind=K.LATE_FEE)
        .filter(period__due_date__lt=today)
    )
    for invoice in candidates:
        deadline = invoice.period.late_after
        if today <= deadline:
            continue
        # Never auto-charge a late fee on an invoice the household got after its due date
        # (e.g. billed by hand late, or caught up after a scheduler outage).
        if timezone.localtime(invoice.created_at).date() > invoice.period.due_date:
            continue
        # Judge lateness as of the deadline, so a late scheduler run doesn't forgive or
        # penalize anyone: payments dated after the deadline were late.
        unpaid = ledger.invoice_unpaid(_lines(invoice.home), invoice.id, as_of=deadline)
        if unpaid <= 0:
            continue
        fee = late_fee_amount(settings, unpaid)
        if fee <= 0:
            continue
        try:
            with transaction.atomic():
                LedgerEntry.objects.create(
                    home=invoice.home,
                    date=deadline + timedelta(days=1),
                    kind=K.LATE_FEE,
                    amount_cents=fee,
                    invoice=invoice,
                    memo=f"Late fee: {invoice.period.label}",
                )
                audit.record(None, "dues.late_fee_applied", invoice, {"amount": format_money(fee)})
            applied += 1
        except IntegrityError:
            continue
    return {"late_fees_applied": applied}


def _send_reminder(invoice, kind, sequence, today):
    recipients = household_emails(invoice.home)
    if not recipients:
        return False
    status = invoice_status(invoice, today)
    try:
        with transaction.atomic():
            ReminderLog.objects.create(
                invoice=invoice, kind=kind, sequence=sequence, recipients=", ".join(recipients)
            )
            send_templated(
                recipients,
                f"dues/email/{kind}",
                {
                    "invoice": invoice,
                    "status": status,
                    "balance": balance(invoice.home),
                    "dues": DuesSettings.load(),
                    "invoice_url": absolute_url(invoice.get_public_url()),
                    "account_url": absolute_url(reverse("dues:my_account")),
                },
            )
    except IntegrityError:
        return False  # already sent
    except Exception:
        log.exception("Failed to send %s reminder for invoice %s", kind, invoice.pk)
        return False  # rolled back; we'll try again tomorrow
    return True


def send_reminders(today):
    settings = DuesSettings.load()
    sent = {"upcoming": 0, "overdue": 0}
    open_invoices = Invoice.objects.select_related("period", "home").filter(
        home__is_active=True, period__due_date__gte=today - timedelta(days=730)
    )
    for invoice in open_invoices:
        status = invoice_status(invoice, today)
        if status.state == "paid":
            continue
        period = invoice.period
        if settings.reminder_days_before and (
            period.due_date - timedelta(days=settings.reminder_days_before) <= today < period.due_date
        ):
            if _send_reminder(invoice, ReminderLog.Kind.UPCOMING, 0, today):
                sent["upcoming"] += 1
        if (
            settings.overdue_reminder_every_days
            and settings.overdue_reminder_max
            and today > period.late_after
        ):
            days_late = (today - period.late_after).days - 1
            seq = min(days_late // settings.overdue_reminder_every_days, settings.overdue_reminder_max - 1)
            if not ReminderLog.objects.filter(
                invoice=invoice, kind=ReminderLog.Kind.OVERDUE, sequence__gte=seq
            ).exists():
                if _send_reminder(invoice, ReminderLog.Kind.OVERDUE, seq, today):
                    sent["overdue"] += 1
    return sent
