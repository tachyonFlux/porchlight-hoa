import csv
from datetime import timedelta

import segno
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.community.models import Home, Residency
from apps.community.services import is_household_member
from apps.core import audit
from apps.core.models import HOASettings
from apps.core.pdf import render_pdf
from apps.core.permissions import board_required, treasurer_required
from apps.core.ratelimit import rate_limit
from apps.core.utils import absolute_url, is_int

from . import periods, services
from .forms import (
    AdjustmentForm,
    DuesSettingsForm,
    NewPeriodForm,
    PaymentForm,
    PeriodBillForm,
    VoidForm,
    WaiveForm,
)
from .models import BillingPeriod, DuesSettings, Invoice, Payment


def _can_view_home(user, home):
    return user.is_board or is_household_member(user, home)


def _invoice_context(invoice, public=False):
    url = absolute_url(invoice.get_public_url())
    return {
        "invoice": invoice,
        "status": services.invoice_status(invoice),
        # The public (QR) page shows only this invoice, never the household's other charges.
        "balance": None if public else services.balance(invoice.home),
        "dues": DuesSettings.load(),
        "hoa": HOASettings.load(),
        "public_url": url,
        "qr_svg": segno.make(url, error="m").svg_inline(scale=3, dark="#000"),
    }


# ------------------------------------------------------------------ residents


@login_required
def my_account(request):
    homes = [r.home for r in Residency.objects.current().filter(user=request.user).select_related("home")]
    accounts = []
    for home in homes:
        rows = services.ledger_rows(home)
        lines = services.lines_for_homes([home.pk])[home.pk]
        open_invoices = [
            s
            for s in (services.invoice_status(i, lines=lines) for i in home.invoices.select_related("period"))
            if s.state != "paid"
        ]
        accounts.append(
            {"home": home, "balance": services.balance(home), "rows": rows[:50], "open": open_invoices}
        )
    return render(request, "dues/my_account.html", {"accounts": accounts, "dues": DuesSettings.load()})


@rate_limit("invoice-public", limit=60, period=3600, methods=("GET",))
def invoice_public(request, token):
    """Reached from the QR code on a paper invoice. Shows only what's printed on it."""
    invoice = get_object_or_404(Invoice.objects.select_related("home", "period"), public_token=token)
    if timezone.localdate() > invoice.public_link_expires:
        response = render(request, "dues/invoice_link_expired.html", status=410)
    else:
        ctx = _invoice_context(invoice, public=True)
        ctx["can_sign_in"] = not request.user.is_authenticated
        response = render(request, "dues/invoice_public.html", ctx)
    response["Cache-Control"] = "private, no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


@login_required
def invoice_detail(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related("home", "period"), pk=pk)
    if not _can_view_home(request.user, invoice.home):
        raise PermissionDenied
    return render(request, "dues/invoice_detail.html", _invoice_context(invoice))


@login_required
def invoice_pdf(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related("home", "period"), pk=pk)
    if not _can_view_home(request.user, invoice.home):
        raise PermissionDenied
    return _pdf_response([invoice], f"invoice-{invoice.number}.pdf")


def _render_invoices_html(invoices):
    return render_to_string("dues/invoice_print.html", {"pages": [_invoice_context(i) for i in invoices]})


def _pdf_response(invoices, filename):
    pdf = render_pdf(_render_invoices_html(invoices))
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


# ------------------------------------------------------------------ board


@board_required
def overview(request):
    """Treasurer report: who has paid for a period, who hasn't."""
    all_periods = BillingPeriod.objects.all()
    period = None
    if is_int(request.GET.get("period", "")):
        period = all_periods.filter(pk=int(request.GET["period"])).first()
    period = period or all_periods.first()
    statuses = (
        sorted(services.statuses_for_period(period), key=lambda s: s.invoice.home.address) if period else []
    )
    state = request.GET.get("state", "")
    shown = [s for s in statuses if s.state == state] if state else statuses
    totals = {
        "billed": sum(s.invoice.amount_cents + s.late_fee_cents for s in statuses),
        "collected": sum(s.paid_cents for s in statuses),
        "outstanding": sum(s.unpaid_cents for s in statuses),
        "paid": sum(1 for s in statuses if s.state == "paid"),
        "open": sum(1 for s in statuses if s.state == "open"),
        "overdue": sum(1 for s in statuses if s.state == "overdue"),
    }
    balances = services.balances_by_home()
    owing = sorted(
        ((h, balances[h.pk]) for h in Home.objects.filter(pk__in=balances) if balances[h.pk] > 0),
        key=lambda pair: -pair[1],
    )
    return render(
        request,
        "dues/overview.html",
        {
            "period": period,
            "periods": all_periods,
            "statuses": shown,
            "state": state,
            "totals": totals,
            "owing": owing,
            "total_owed": sum(b for _, b in owing),
            "dues": DuesSettings.load(),
            "recent_payments": Payment.objects.select_related("home")[:8],
        },
    )


@board_required
def report_csv(request):
    """CSV of every active home: current balance, and status for one period if chosen."""
    period = None
    if is_int(request.GET.get("period", "")):
        period = get_object_or_404(BillingPeriod, pk=int(request.GET["period"]))
    statuses = {s.invoice.home_id: s for s in services.statuses_for_period(period)} if period else {}
    balances = services.balances_by_home()
    # Active homes, plus inactive ones that still have a balance, so totals match the report.
    with_balance = [pk for pk, cents in balances.items() if cents]
    homes = Home.objects.filter(Q(is_active=True) | Q(pk__in=with_balance)).prefetch_related(
        Prefetch(
            "residencies", queryset=Residency.objects.current().select_related("user"), to_attr="current"
        )
    )
    response = HttpResponse(content_type="text/csv")
    name = f"dues-{period.start:%Y-%m}" if period else f"balances-{timezone.localdate():%Y-%m-%d}"
    response["Content-Disposition"] = f'attachment; filename="{name}.csv"'
    writer = csv.writer(response)
    header = ["address", "lot", "active", "residents", "emails", "current_balance"]
    if period:
        header += ["period", "invoiced", "late_fee", "paid", "unpaid", "status"]
    writer.writerow(header)
    for home in homes:
        people = home.current
        row = [
            _csv_safe(home.address),
            _csv_safe(home.lot_number),
            "yes" if home.is_active else "no",
            _csv_safe("; ".join(str(r.user) for r in people)),
            _csv_safe("; ".join(r.user.email for r in people)),
            f"{balances.get(home.pk, 0) / 100:.2f}",
        ]
        if period:
            s = statuses.get(home.pk)
            if s:
                row += [
                    period.label,
                    f"{s.invoice.amount_cents / 100:.2f}",
                    f"{s.late_fee_cents / 100:.2f}",
                    f"{s.paid_cents / 100:.2f}",
                    f"{s.unpaid_cents / 100:.2f}",
                    s.state,
                ]
            else:
                row += [period.label, "", "", "", "", "not billed"]
        writer.writerow(row)
    audit.record(request, "dues.report_exported", period, {"rows": len(homes)})
    return response


def _csv_safe(value):
    """Stop spreadsheet apps from treating a cell as a formula."""
    value = str(value or "")
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


@board_required
def home_ledger(request, pk):
    home = get_object_or_404(Home, pk=pk)
    invoices = [services.invoice_status(i) for i in home.invoices.select_related("period")]
    return render(
        request,
        "dues/home_ledger.html",
        {
            "home": home,
            "balance": services.balance(home),
            "rows": services.ledger_rows(home),
            "invoices": invoices,
            "payments": home.payments.all(),
            "payment_form": PaymentForm(home=home),
            "adjustment_form": AdjustmentForm(),
        },
    )


@board_required
def payment_new(request):
    home = None
    if is_int(request.GET.get("home", "")):
        home = Home.objects.filter(pk=int(request.GET["home"])).first()
    form = PaymentForm(request.POST or None, home=home)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        payment = services.record_payment(
            d["home"],
            d["amount_cents"],
            d["method"],
            d["received_on"],
            d["check_number"],
            d["note"],
            actor=request.user,
            request=request,
        )
        messages.success(request, f"Recorded {payment}.")
        if request.POST.get("next") == "ledger":
            return redirect("dues:home_ledger", pk=d["home"].pk)
        return redirect("dues:payment_new")
    return render(
        request,
        "dues/payment_form.html",
        {"form": form, "home": home, "recent": Payment.objects.select_related("home")[:10]},
    )


@board_required
@require_POST
def adjustment(request, pk):
    home = get_object_or_404(Home, pk=pk)
    form = AdjustmentForm(request.POST)
    if form.is_valid():
        services.add_adjustment(
            home,
            form.signed_cents(),
            form.cleaned_data["memo"],
            form.cleaned_data["entry_date"],
            actor=request.user,
            request=request,
        )
        messages.success(request, "Ledger updated.")
    else:
        messages.error(request, "Please fill in the amount, memo, and date.")
    return redirect("dues:home_ledger", pk=home.pk)


@board_required
def payment_void(request, pk):
    payment = get_object_or_404(Payment.objects.select_related("home"), pk=pk, voided_at__isnull=True)
    form = VoidForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        services.void_payment(payment, form.cleaned_data["reason"], actor=request.user, request=request)
        messages.success(request, "Payment voided.")
        return redirect("dues:home_ledger", pk=payment.home_id)
    return render(
        request,
        "dues/confirm_form.html",
        {
            "form": form,
            "title": "Void payment",
            "object": payment,
            "button": "Void payment",
            "back": payment.home_id,
            "explain": "The payment stays in the records but no longer counts. Use this for bounced "
            "checks or payments entered by mistake.",
        },
    )


@board_required
def waive_late_fee(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related("home", "period"), pk=pk)
    form = WaiveForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.waive_late_fee(
                invoice, actor=request.user, request=request, reason=form.cleaned_data["reason"]
            )
            messages.success(request, "Late fee waived.")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("dues:home_ledger", pk=invoice.home_id)
    return render(
        request,
        "dues/confirm_form.html",
        {
            "form": form,
            "title": "Waive late fee",
            "object": invoice,
            "button": "Waive late fee",
            "back": invoice.home_id,
            "explain": "Adds a credit that cancels this invoice's late fee.",
        },
    )


@treasurer_required
def dues_settings(request):
    settings_obj = DuesSettings.load()
    form = DuesSettingsForm(request.POST or None, instance=settings_obj)
    if request.method == "POST" and form.is_valid():
        changed = {f: str(form.cleaned_data[f]) for f in form.changed_data}
        form.save()
        audit.record(request, "dues.settings_updated", settings_obj, changed)
        payment_fields = {"payable_to", "payment_instructions"} & set(changed)
        if payment_fields:
            services.alert_board(
                "Dues payment instructions changed",
                f"{request.user} changed how residents are told to pay. New values: "
                + "; ".join(f"{f.replace('_', ' ')}: {changed[f]!r}" for f in sorted(payment_fields)),
                exclude=request.user,
            )
        messages.success(request, "Dues settings saved. Changes apply to periods created from now on.")
        return redirect("dues:settings")
    preview = None
    s = settings_obj
    if s.billing_start_date:
        upcoming = periods.periods_to_issue(
            s.billing_start_date + timedelta(days=800),
            frequency=s.frequency,
            anchor_month=s.anchor_month,
            due_day=s.due_day,
            lead_days=s.invoice_lead_days,
            billing_start=s.billing_start_date,
        )[:3]
        preview = [
            {
                "label": periods.period_label(p, s.frequency),
                "issue": p.issue_date(s.invoice_lead_days),
                "due": p.due_date,
                "late_after": p.due_date + timedelta(days=s.grace_days),
            }
            for p in upcoming
        ]
    return render(request, "dues/settings.html", {"form": form, "preview": preview})


@board_required
def period_list(request):
    periods_qs = BillingPeriod.objects.all()
    form = NewPeriodForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        s = DuesSettings.load()
        if s.amount_cents <= 0:
            messages.error(request, "Set the dues amount in dues settings first.")
            return redirect("dues:settings")
        spec = periods.period_containing(form.cleaned_data["date"], s.frequency, s.anchor_month, s.due_day)
        try:
            period = services.ensure_period(spec, s)
        except services.PeriodOverlapError as exc:
            messages.error(request, f"{exc} Pick a date in a period that doesn't exist yet.")
            return redirect("dues:period_list")
        audit.record(request, "dues.period_created", period)
        return redirect("dues:period_detail", pk=period.pk)
    return render(request, "dues/period_list.html", {"periods": periods_qs, "form": form})


@board_required
def period_detail(request, pk):
    period = get_object_or_404(BillingPeriod, pk=pk)
    form = PeriodBillForm(request.POST or None, period=period)
    if request.method == "POST" and form.is_valid():
        created = services.bill_homes(period, form.cleaned_data["homes"], actor=request.user, request=request)
        messages.success(request, f"Created {len(created)} invoice{'s' if len(created) != 1 else ''}.")
        return redirect("dues:period_detail", pk=period.pk)
    return render(
        request,
        "dues/period_detail.html",
        {"period": period, "form": form, "invoice_count": period.invoices.count()},
    )


@board_required
def period_print(request, pk):
    period = get_object_or_404(BillingPeriod, pk=pk)
    invoices = list(period.invoices.select_related("home", "period").order_by("home__address"))
    if request.GET.get("unpaid"):
        invoices = [s.invoice for s in services.statuses_for_period(period) if s.state != "paid"]
    if request.GET.get("format") == "pdf":
        return _pdf_response(invoices, f"invoices-{period.start:%Y-%m}.pdf")
    return HttpResponse(_render_invoices_html(invoices))
