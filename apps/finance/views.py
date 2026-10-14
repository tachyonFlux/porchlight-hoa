import csv
import uuid
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core import audit
from apps.core.pdf import render_pdf
from apps.core.permissions import board_required, treasurer_required
from apps.dues.models import Payment

from . import parsers, services, simplefin
from .forms import (
    AccountForm,
    BankAccountForm,
    MappingForm,
    RuleForm,
    SimpleFINConnectForm,
    StatementForm,
    UploadForm,
)
from .models import (
    Account,
    BankAccount,
    CategoryRule,
    ImportBatch,
    ImportMapping,
    PaymentMatch,
    SimpleFINAccountLink,
    SimpleFINConnection,
    Transaction,
)

PENDING_KEY = "finance_pending_import"


def _pending_dir():
    path = Path(settings.DATA_DIR) / "imports" / "pending"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ------------------------------------------------------------------ overview


@board_required
def overview(request):
    banks = []
    for bank in BankAccount.objects.filter(is_active=True):
        checks = services.statement_checks(bank)
        banks.append(
            {
                "bank": bank,
                "balance": services.book_balance(bank, timezone.localdate()),
                "last_check": checks[0] if checks else None,
            }
        )
    year = timezone.localdate().year
    return render(
        request,
        "finance/overview.html",
        {
            "banks": banks,
            "uncategorized": Transaction.objects.filter(account__isnull=True).count(),
            "unmatched_deposits": Transaction.objects.filter(
                amount_cents__gt=0, payment_matches__isnull=True, date__year__gte=year - 1
            ).count(),
            "suggested": Transaction.objects.filter(payment_matches__status=PaymentMatch.Status.SUGGESTED)
            .distinct()
            .count(),
            "batches": ImportBatch.objects.select_related("bank_account", "created_by")[:8],
            "simplefin_enabled": simplefin.is_enabled(),
            "simplefin": SimpleFINConnection.objects.first(),
        },
    )


@treasurer_required
def bank_account_form(request, pk=None):
    bank = get_object_or_404(BankAccount, pk=pk) if pk else None
    form = BankAccountForm(
        request.POST or None,
        instance=bank,
        initial={} if bank else {"opening_date": timezone.localdate().replace(month=1, day=1)},
    )
    if request.method == "POST" and form.is_valid():
        bank = form.save()
        audit.record(request, "finance.bank_account_saved", bank, {"fields": form.changed_data})
        messages.success(request, "Bank account saved.")
        return redirect("finance:overview")
    return render(
        request,
        "finance/simple_form.html",
        {
            "form": form,
            "title": "Edit bank account" if bank else "Add a bank account",
            "back": "finance:overview",
        },
    )


# ------------------------------------------------------------------ chart of accounts & rules


@board_required
def chart(request):
    return render(
        request, "finance/chart.html", {"accounts": Account.objects.annotate(n=Count("transactions"))}
    )


@treasurer_required
def account_form(request, pk=None):
    account = get_object_or_404(Account, pk=pk) if pk else None
    form = AccountForm(request.POST or None, instance=account)
    if request.method == "POST" and form.is_valid():
        account = form.save()
        audit.record(request, "finance.account_saved", account, {"fields": form.changed_data})
        return redirect("finance:chart")
    return render(
        request,
        "finance/simple_form.html",
        {"form": form, "title": "Edit category" if account else "Add a category", "back": "finance:chart"},
    )


@board_required
def rules(request):
    form = RuleForm(request.POST or None)
    if request.method == "POST":
        if not (request.user.is_admin or request.user.holds_title("treasurer")):
            messages.error(request, "Only the treasurer or an admin can change rules.")
            return redirect("finance:rules")
        if request.POST.get("delete", "").isdigit():
            CategoryRule.objects.filter(pk=int(request.POST["delete"])).delete()
            messages.success(request, "Rule deleted.")
            return redirect("finance:rules")
        if "apply" in request.POST:
            n = services.recategorize_uncategorized()
            messages.success(request, f"Categorized {n} transaction{'s' if n != 1 else ''}.")
            return redirect("finance:rules")
        if form.is_valid():
            form.save()
            n = services.recategorize_uncategorized()
            messages.success(
                request, f"Rule added; it categorized {n} existing transaction{'s' if n != 1 else ''}."
            )
            return redirect("finance:rules")
    return render(
        request, "finance/rules.html", {"form": form, "rules": CategoryRule.objects.select_related("account")}
    )


# ------------------------------------------------------------------ import


@treasurer_required
def import_upload(request):
    form = UploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        bank = form.cleaned_data["bank_account"]
        upload = form.cleaned_data["file"]
        raw = upload.read(parsers.MAX_FILE_BYTES + 1)
        try:
            if upload.name.lower().endswith((".ofx", ".qfx")) or raw.lstrip()[:20].upper().startswith(
                (b"OFXHEADER", b"<?XML")
            ):
                last4, rows = parsers.parse_ofx(raw)
                if bank.last4 and last4 and last4 != bank.last4:
                    messages.warning(
                        request, f"Heads up: that file is for an account ending in {last4}, not {bank.last4}."
                    )
                batch = services.import_rows(
                    rows, bank, ImportBatch.Source.OFX, upload.name, request.user, request
                )
                return redirect("finance:import_done", pk=batch.pk)
            text = parsers.decode(raw)
            headers, _ = parsers.read_csv(text)
        except parsers.ParseError as exc:
            form.add_error("file", str(exc))
        else:
            # Keep the file out of the session (it can be large) and out of the web root.
            token = uuid.uuid4().hex
            (_pending_dir() / f"{token}.csv").write_text(text, encoding="utf-8")
            request.session[PENDING_KEY] = {"token": token, "bank": bank.pk, "name": upload.name[:200]}
            return redirect("finance:import_map")
    return render(request, "finance/import_upload.html", {"form": form})


def _load_pending(request):
    pending = request.session.get(PENDING_KEY)
    if not pending:
        return None, None, None
    path = _pending_dir() / f"{pending['token']}.csv"
    if not pending["token"].isalnum() or not path.exists():
        return None, None, None
    return pending, path, path.read_text(encoding="utf-8")


@treasurer_required
def import_map(request):
    pending, path, text = _load_pending(request)
    if pending is None:
        messages.error(request, "That upload expired. Please upload the file again.")
        return redirect("finance:import_upload")
    headers, records = parsers.read_csv(text)
    saved = ImportMapping.objects.filter(columns__isnull=False)
    chosen = None
    if request.GET.get("mapping", "").isdigit():
        chosen = saved.filter(pk=int(request.GET["mapping"])).first()
    initial_mapping = chosen.columns if chosen else parsers.guess_mapping(headers)
    form = MappingForm(
        request.POST or None,
        headers=headers,
        initial_mapping=initial_mapping,
        initial={"date_format": chosen.date_format if chosen else ""},
    )
    rows, error = None, None
    if form.is_bound and form.is_valid():
        try:
            rows = parsers.apply_mapping(
                headers,
                records,
                form.mapping(),
                form.cleaned_data["date_format"] or None,
                form.cleaned_data["flip_sign"],
            )
        except parsers.ParseError as exc:
            error = str(exc)
        if rows is not None and "confirm" in request.POST:
            bank = get_object_or_404(BankAccount, pk=pending["bank"])
            if form.cleaned_data["save_as"]:
                ImportMapping.objects.update_or_create(
                    name=form.cleaned_data["save_as"].strip(),
                    defaults={"columns": form.mapping(), "date_format": form.cleaned_data["date_format"]},
                )
            batch = services.import_rows(
                rows, bank, ImportBatch.Source.CSV, pending["name"], request.user, request
            )
            path.unlink(missing_ok=True)
            request.session.pop(PENDING_KEY, None)
            return redirect("finance:import_done", pk=batch.pk)
    return render(
        request,
        "finance/import_map.html",
        {
            "form": form,
            "headers": headers,
            "sample": records[:5],
            "rows": rows[:25] if rows else None,
            "ok_count": sum(r.ok for r in rows) if rows else 0,
            "bad": [r for r in rows if not r.ok][:20] if rows else [],
            "error": error,
            "saved": saved,
            "filename": pending["name"],
        },
    )


@board_required
def import_done(request, pk):
    batch = get_object_or_404(ImportBatch, pk=pk)
    return render(request, "finance/import_done.html", {"batch": batch})


# ------------------------------------------------------------------ transactions


def _can_edit(user):
    return user.is_admin or user.holds_title("treasurer")


def _without_page(params):
    params = params.copy()
    params.pop("page", None)
    return params.urlencode()


@board_required
def transactions(request):
    qs = (
        Transaction.objects.select_related("account", "bank_account")
        .annotate(matches=Count("payment_matches"))
        .order_by("-date", "-id")
    )
    f = request.GET
    if f.get("year", "").isdigit():
        qs = qs.filter(date__year=int(f["year"]))
    if f.get("bank", "").isdigit():
        qs = qs.filter(bank_account_id=int(f["bank"]))
    if f.get("account") == "none":
        qs = qs.filter(account__isnull=True)
    elif f.get("account", "").isdigit():
        qs = qs.filter(account_id=int(f["account"]))
    if f.get("q"):
        qs = qs.filter(Q(description__icontains=f["q"]) | Q(memo__icontains=f["q"]) | Q(check_number=f["q"]))
    page = Paginator(qs, 100).get_page(f.get("page"))
    return render(
        request,
        "finance/transactions.html",
        {
            "page": page,
            "accounts": Account.objects.filter(is_active=True),
            "banks": BankAccount.objects.all(),
            "years": services.years_with_data(),
            "f": f,
            "querystring": _without_page(f),
            "can_edit": _can_edit(request.user),
        },
    )


@treasurer_required
@require_POST
def categorize(request, pk):
    txn = get_object_or_404(Transaction, pk=pk)
    account = None
    if request.POST.get("account", "").isdigit():
        account = get_object_or_404(Account, pk=int(request.POST["account"]))
    rule = services.set_category(
        txn,
        account,
        request.user,
        make_rule=bool(request.POST.get("make_rule")),
        pattern=request.POST.get("pattern", ""),
        request=request,
    )
    if request.htmx:
        txn.refresh_from_db()
        return render(
            request,
            "finance/_txn_category.html",
            {"t": txn, "accounts": Account.objects.filter(is_active=True), "rule": rule},
        )
    if rule:
        messages.success(request, f"Saved, and added a rule: {rule}.")
    return redirect(request.POST.get("next") or "finance:transactions")


# ------------------------------------------------------------------ reconciliation


@board_required
def reconcile(request):
    deposits = (
        Transaction.objects.filter(amount_cents__gt=0)
        .exclude(account__type=Account.Type.TRANSFER)
        .prefetch_related("payment_matches__payment__home")
        .select_related("bank_account", "account")
        .order_by("-date")
    )
    suggested = [d for d in deposits.filter(payment_matches__status=PaymentMatch.Status.SUGGESTED).distinct()]
    unmatched = deposits.filter(payment_matches__isnull=True)[:100]
    payments = Payment.objects.filter(voided_at__isnull=True, bank_match__isnull=True).select_related("home")
    return render(
        request,
        "finance/reconcile.html",
        {
            "suggested": suggested,
            "unmatched": unmatched,
            "payments": payments[:200],
            "payments_total": payments.aggregate(t=Sum("amount_cents"))["t"] or 0,
        },
    )


@treasurer_required
@require_POST
def reconcile_action(request, pk):
    action = request.POST.get("action")
    if action == "auto":  # not tied to one deposit
        n = services.auto_match_deposits()
        messages.success(request, f"Found {n} new match{'es' if n != 1 else ''}.")
        return redirect("finance:reconcile")
    deposit = get_object_or_404(Transaction, pk=pk, amount_cents__gt=0)
    if action == "confirm":
        services.confirm_match(deposit, request.user, request)
        messages.success(request, "Match confirmed.")
    elif action == "unmatch":
        services.unmatch(deposit, request.user, request)
        messages.success(request, "Match removed.")
    elif action == "match":
        ids = [int(x) for x in request.POST.getlist("payments") if x.isdigit()]
        chosen = list(
            Payment.objects.filter(pk__in=ids, voided_at__isnull=True).filter(
                Q(bank_match__isnull=True) | Q(bank_match__transaction=deposit)
            )
        )
        try:
            services.manual_match(deposit, chosen, request.user, request)
            messages.success(request, "Deposit matched.")
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect("finance:reconcile")


@board_required
def statements(request, pk):
    bank = get_object_or_404(BankAccount, pk=pk)
    form = StatementForm(request.POST or None)
    if request.method == "POST":
        if not (request.user.is_admin or request.user.holds_title("treasurer")):
            raise Http404
        if form.is_valid():
            statement = form.save(commit=False)
            statement.bank_account = bank
            statement.save()
            audit.record(request, "finance.statement_added", bank, {"date": str(statement.statement_date)})
            return redirect("finance:statements", pk=bank.pk)
    return render(
        request,
        "finance/statements.html",
        {"bank": bank, "checks": services.statement_checks(bank), "form": form},
    )


# ------------------------------------------------------------------ tax ledger


def _year(request):
    raw = request.GET.get("year", "")
    return int(raw) if raw.isdigit() else timezone.localdate().year - 1


@board_required
def ledger(request):
    year = _year(request)
    return render(
        request,
        "finance/ledger.html",
        {
            "year": year,
            "years": services.years_with_data(),
            "summary": services.ledger_summary(year),
            "hoa_print": False,
        },
    )


@board_required
def ledger_csv(request):
    year = _year(request)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="ledger-{year}.csv"'
    writer = csv.writer(response)
    writer.writerow(
        [
            "date",
            "description",
            "check_number",
            "amount",
            "category_code",
            "category",
            "tax_class",
            "bank_account",
        ]
    )
    for t in services.ledger_transactions(year):
        writer.writerow(
            [
                t.date.isoformat(),
                _csv_safe(t.description),
                _csv_safe(t.check_number),
                f"{t.amount_cents / 100:.2f}",
                t.account.code if t.account else "",
                _csv_safe(t.account.name) if t.account else "UNCATEGORIZED",
                t.account.tax_class if t.account else "",
                _csv_safe(str(t.bank_account or "")),
            ]
        )
    audit.record(request, "finance.ledger_exported", None, {"year": year})
    return response


def _csv_safe(value):
    value = str(value or "")
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


@board_required
def ledger_print(request):
    year = _year(request)
    ctx = {
        "year": year,
        "summary": services.ledger_summary(year),
        "transactions": services.ledger_transactions(year),
        "generated": timezone.now(),
    }
    html = render_to_string("finance/ledger_print.html", ctx, request=request)
    if request.GET.get("format") == "pdf":
        response = HttpResponse(render_pdf(html), content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="ledger-{year}.pdf"'
        return response
    return HttpResponse(html)


# ------------------------------------------------------------------ SimpleFIN


@treasurer_required
def simplefin_settings(request):
    if not simplefin.is_enabled():
        return render(request, "finance/simplefin_off.html")
    conn = SimpleFINConnection.objects.first()
    form = SimpleFINConnectForm(request.POST or None)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "connect" and form.is_valid():
            try:
                services.simplefin_connect(form.cleaned_data["setup_token"], request.user, request)
                messages.success(
                    request, "Connected. Now choose which bank account each SimpleFIN account feeds."
                )
            except simplefin.SimpleFINError as exc:
                messages.error(request, str(exc))
            return redirect("finance:simplefin")
        if action == "disconnect":
            services.simplefin_disconnect(request.user, request)
            messages.success(request, "Disconnected. The saved access was deleted.")
            return redirect("finance:simplefin")
        if action == "sync":
            result = services.simplefin_sync()
            if "simplefin_error" in result:
                messages.error(request, result["simplefin_error"])
            else:
                messages.success(request, f"Imported {result.get('simplefin_imported', 0)} new transactions.")
            return redirect("finance:simplefin")
        if action == "link" and conn:
            for link in conn.links.all():
                value = request.POST.get(f"link_{link.pk}", "")
                link.bank_account = (
                    BankAccount.objects.filter(pk=int(value)).first() if value.isdigit() else None
                )
                link.save(update_fields=["bank_account"])
            audit.record(request, "finance.simplefin_links_saved", conn)
            messages.success(request, "Saved.")
            return redirect("finance:simplefin")
    return render(
        request,
        "finance/simplefin.html",
        {
            "conn": conn,
            "form": form,
            "links": SimpleFINAccountLink.objects.filter(connection=conn) if conn else [],
            "banks": BankAccount.objects.filter(is_active=True),
        },
    )
