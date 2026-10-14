"""Finance: importing transactions, categorizing, reconciling, and the tax ledger."""

import itertools
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from django.db import IntegrityError, transaction
from django.db.models import Sum

from apps.core import audit

from .models import (
    Account,
    BankAccount,
    CategoryRule,
    ImportBatch,
    PaymentMatch,
    StatementBalance,
    Transaction,
)

# ------------------------------------------------------------------ import


@transaction.atomic
def import_rows(rows, bank_account, source, filename="", user=None, request=None):
    """Save parsed rows, skipping ones already imported. Returns the ImportBatch."""
    batch = ImportBatch.objects.create(
        bank_account=bank_account,
        source=source,
        filename=filename[:200],
        created_by=user,
        rows_total=len(rows),
    )
    rules = list(
        CategoryRule.objects.filter(is_active=True, account__is_active=True).select_related("account")
    )
    existing = set(
        Transaction.objects.filter(
            bank_account=bank_account, dedupe_key__in=[r.dedupe_key for r in rows if r.ok]
        ).values_list("dedupe_key", flat=True)
    )
    for row in rows:
        if not row.ok:
            batch.rows_error += 1
            continue
        if row.dedupe_key in existing:
            batch.rows_duplicate += 1
            continue
        txn = Transaction(
            bank_account=bank_account,
            date=row.date,
            description=row.description,
            amount_cents=row.amount_cents,
            source=source,
            dedupe_key=row.dedupe_key,
            fitid=row.fitid,
            check_number=row.check_number,
            memo=row.memo,
            batch=batch,
        )
        _apply_rules(txn, rules)
        try:
            with transaction.atomic():
                txn.save()
        except IntegrityError:  # imported concurrently
            batch.rows_duplicate += 1
            continue
        existing.add(row.dedupe_key)
        batch.rows_imported += 1
    batch.save()
    audit.record(
        request or user,
        "finance.imported",
        batch,
        {"imported": batch.rows_imported, "duplicates": batch.rows_duplicate, "errors": batch.rows_error},
    )
    if batch.rows_imported:
        auto_match_deposits()
    return batch


# ------------------------------------------------------------------ categorizing


def _apply_rules(txn, rules):
    for rule in rules:
        if rule.matches(txn.description, txn.amount_cents):
            txn.account, txn.rule, txn.categorized_by = rule.account, rule, Transaction.Categorized.RULE
            return True
    return False


def recategorize_uncategorized():
    """Run rules over transactions nobody has categorized yet (e.g. after adding a rule)."""
    rules = list(
        CategoryRule.objects.filter(is_active=True, account__is_active=True).select_related("account")
    )
    changed = 0
    for txn in Transaction.objects.filter(account__isnull=True):
        if _apply_rules(txn, rules):
            txn.save(update_fields=["account", "rule", "categorized_by"])
            changed += 1
    return changed


def set_category(txn, account, user, make_rule=False, pattern="", request=None):
    old = txn.account
    txn.account = account
    txn.rule = None
    txn.categorized_by = Transaction.Categorized.MANUAL if account else Transaction.Categorized.NONE
    txn.save(update_fields=["account", "rule", "categorized_by"])
    audit.record(
        request or user, "finance.categorized", txn, {"from": str(old or ""), "to": str(account or "")}
    )
    rule = None
    if make_rule and account and pattern.strip():
        rule = CategoryRule.objects.create(
            pattern=pattern.strip()[:100],
            account=account,
            direction=CategoryRule.Direction.IN if txn.amount_cents > 0 else CategoryRule.Direction.OUT,
        )
        recategorize_uncategorized()
    return rule


# ------------------------------------------------------------------ reconciliation

MATCH_WINDOW_DAYS = 14  # deposits usually land within two weeks of the check arriving
MAX_COMBINATION = 6  # checks per deposit we'll try to combine
MAX_CANDIDATES = 18


def _unmatched_payments():
    from apps.dues.models import Payment

    return Payment.objects.filter(voided_at__isnull=True, bank_match__isnull=True).select_related("home")


def _candidates(deposit, payments):
    start = deposit.date - timedelta(days=MATCH_WINDOW_DAYS)
    return [p for p in payments if start <= p.received_on <= deposit.date]


def find_match(deposit, payments):
    """Payments that add up to the deposit: a single check (preferring a check-number hit),
    otherwise the smallest combination of nearby payments. Returns (payments, unique)."""
    cands = _candidates(deposit, payments)
    singles = [p for p in cands if p.amount_cents == deposit.amount_cents]
    if deposit.check_number:
        by_number = [p for p in singles if p.check_number and p.check_number == deposit.check_number]
        if len(by_number) == 1:
            return by_number, True
    if singles:
        return [singles[0]], len(singles) == 1
    cands = sorted(cands, key=lambda p: (deposit.date - p.received_on, p.pk))[:MAX_CANDIDATES]
    for size in range(2, min(MAX_COMBINATION, len(cands)) + 1):
        hits = [
            c
            for c in itertools.combinations(cands, size)
            if sum(p.amount_cents for p in c) == deposit.amount_cents
        ]
        if hits:
            return list(hits[0]), len(hits) == 1
    return [], False


@transaction.atomic
def auto_match_deposits():
    """Suggest (or, when unambiguous, confirm) matches between deposits and dues payments."""
    payments = list(_unmatched_payments())
    deposits = Transaction.objects.filter(amount_cents__gt=0, payment_matches__isnull=True).order_by(
        "date", "id"
    )
    created = 0
    for deposit in deposits:
        if not payments:
            break
        found, unique = find_match(deposit, payments)
        if not found:
            continue
        status = (
            PaymentMatch.Status.CONFIRMED if unique and len(found) == 1 else PaymentMatch.Status.SUGGESTED
        )
        for p in found:
            PaymentMatch.objects.create(transaction=deposit, payment=p, status=status)
            payments.remove(p)
            created += 1
    return created


def confirm_match(deposit, user, request=None):
    PaymentMatch.objects.filter(transaction=deposit).update(
        status=PaymentMatch.Status.CONFIRMED, confirmed_by=user
    )
    audit.record(request or user, "finance.match_confirmed", deposit)


def unmatch(deposit, user, request=None):
    PaymentMatch.objects.filter(transaction=deposit).delete()
    audit.record(request or user, "finance.match_removed", deposit)


def manual_match(deposit, payments, user, request=None):
    total = sum(p.amount_cents for p in payments)
    if total != deposit.amount_cents:
        raise ValueError("Those payments don't add up to the deposit.")
    with transaction.atomic():
        PaymentMatch.objects.filter(transaction=deposit).delete()
        for p in payments:
            PaymentMatch.objects.create(
                transaction=deposit, payment=p, status=PaymentMatch.Status.CONFIRMED, confirmed_by=user
            )
    audit.record(request or user, "finance.match_manual", deposit, {"payments": [p.pk for p in payments]})


def book_balance(bank_account, as_of):
    """Opening balance plus every transaction from the opening date through `as_of`."""
    total = (
        Transaction.objects.filter(
            bank_account=bank_account, date__gte=bank_account.opening_date, date__lte=as_of
        ).aggregate(t=Sum("amount_cents"))["t"]
        or 0
    )
    return bank_account.opening_balance_cents + total


@dataclass
class StatementCheck:
    statement: StatementBalance
    book_cents: int

    @property
    def difference(self):
        return self.statement.ending_balance_cents - self.book_cents

    @property
    def ok(self):
        return self.difference == 0


def statement_checks(bank_account):
    return [
        StatementCheck(s, book_balance(bank_account, s.statement_date)) for s in bank_account.statements.all()
    ]


# ------------------------------------------------------------------ tax ledger


@dataclass
class LedgerLine:
    account: Account | None
    income_cents: int = 0  # money in
    expense_cents: int = 0  # money out (positive number)
    count: int = 0

    @property
    def net_cents(self):
        """Net in the account's own direction: refunds reduce an expense, chargebacks reduce income."""
        if self.account and self.account.type == Account.Type.EXPENSE:
            return self.expense_cents - self.income_cents
        return self.income_cents - self.expense_cents


def ledger_transactions(year):
    """Every categorized-or-not transaction in the calendar year, excluding transfers."""
    return (
        Transaction.objects.filter(date__year=year)
        .exclude(account__type=Account.Type.TRANSFER)
        .select_related("account", "bank_account")
        .order_by("date", "id")
    )


def ledger_summary(year):
    lines = defaultdict(lambda: None)
    for txn in ledger_transactions(year):
        key = txn.account_id
        if lines[key] is None:
            lines[key] = LedgerLine(txn.account)
        line = lines[key]
        line.count += 1
        if txn.amount_cents >= 0:
            line.income_cents += txn.amount_cents
        else:
            line.expense_cents += -txn.amount_cents
    result = [line for line in lines.values() if line is not None]
    income = sorted(
        (x for x in result if x.account and x.account.type == Account.Type.INCOME),
        key=lambda x: x.account.code,
    )
    expense = sorted(
        (x for x in result if x.account and x.account.type == Account.Type.EXPENSE),
        key=lambda x: x.account.code,
    )
    uncategorized = next((x for x in result if x.account is None), None)
    return {
        "income": income,
        "expense": expense,
        "uncategorized": uncategorized,
        "total_income": sum(x.net_cents for x in income),
        "total_expense": sum(x.net_cents for x in expense),
        "net": sum(x.net_cents for x in income) - sum(x.net_cents for x in expense),
    }


def years_with_data():
    dates = Transaction.objects.dates("date", "year", order="DESC")
    return [d.year for d in dates] or [date.today().year]


def active_bank_accounts():
    return BankAccount.objects.filter(is_active=True)


# ------------------------------------------------------------------ SimpleFIN


def simplefin_connect(setup_token, user, request=None):
    from . import simplefin
    from .models import SimpleFINConnection

    access_url = simplefin.claim(setup_token)
    SimpleFINConnection.objects.all().delete()  # one connection per HOA
    conn = SimpleFINConnection.objects.create(
        access_url_encrypted=simplefin.encrypt(access_url), created_by=user
    )
    audit.record(request or user, "finance.simplefin_connected", conn)
    simplefin_sync(conn)
    return conn


def simplefin_disconnect(user, request=None):
    from .models import SimpleFINConnection

    SimpleFINConnection.objects.all().delete()
    audit.record(request or user, "finance.simplefin_disconnected", None)


def simplefin_sync(conn=None):
    """Pull recent transactions for every remote account linked to a bank account."""
    from django.utils import timezone

    from . import simplefin
    from .models import SimpleFINAccountLink, SimpleFINConnection

    conn = conn or SimpleFINConnection.objects.first()
    if conn is None or not simplefin.is_enabled():
        return {"simplefin": "off"}
    try:
        accounts, errors = simplefin.fetch_accounts(
            simplefin.decrypt(conn.access_url_encrypted), simplefin.default_since(conn.last_sync_at)
        )
    except simplefin.SimpleFINError as exc:
        conn.last_error = str(exc)
        conn.save(update_fields=["last_error"])
        return {"simplefin_error": str(exc)}
    imported = 0
    for remote in accounts:
        link, _ = SimpleFINAccountLink.objects.get_or_create(
            connection=conn,
            remote_id=str(remote.get("id")),
            defaults={"remote_name": str(remote.get("name", ""))[:200]},
        )
        try:
            link.last_balance_cents = simplefin._cents(remote.get("balance", "0"))
        except Exception:  # noqa: S110 - balance is informational
            pass
        link.save(update_fields=["last_balance_cents"])
        if link.bank_account_id:
            batch = import_rows(
                simplefin.account_rows(remote),
                link.bank_account,
                ImportBatch.Source.SIMPLEFIN,
                filename=link.remote_name,
            )
            imported += batch.rows_imported
    conn.last_sync_at = timezone.now()
    conn.last_error = "; ".join(errors)[:2000]
    conn.save(update_fields=["last_sync_at", "last_error"])
    return {"simplefin_imported": imported}
