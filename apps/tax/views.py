from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.core import audit
from apps.core.money import to_cents
from apps.core.pdf import render_pdf
from apps.core.permissions import board_required, treasurer_required
from apps.core.utils import is_int
from apps.finance.models import Account
from apps.finance.services import years_with_data

from . import services
from .forms import TaxYearConfigForm, WorksheetForm
from .models import TaxYearConfig, Worksheet


def _can_edit(user):
    return user.is_admin or user.holds_title("treasurer")


def _check_year(year):
    if not 1990 <= year <= timezone.localdate().year:
        raise Http404


@board_required
def index(request):
    this_year = timezone.localdate().year
    years = sorted(
        set(years_with_data()) | set(Worksheet.objects.values_list("year", flat=True)), reverse=True
    )
    years = [y for y in years if y <= this_year]
    return render(
        request,
        "tax/index.html",
        {
            "years": [(y, services.due_date(y), Worksheet.objects.filter(year=y).first()) for y in years],
            "default_year": this_year - 1,
            "disclaimer": services.DISCLAIMER,
        },
    )


@board_required
def worksheet(request, year):
    _check_year(year)
    ws = services.get_worksheet(year)
    form = WorksheetForm(request.POST or None, instance=ws)
    if request.method == "POST":
        if not _can_edit(request.user):
            raise Http404
        if form.is_valid():
            ws = form.save(commit=False)
            ws.line_map, ws.nonproperty_accounts, ws.adjustments, errors = _line_inputs(request.POST)
            if errors:
                for e in errors:
                    messages.error(request, e)
            else:
                ws.updated_by = request.user
                ws.save()
                audit.record(request, "tax.worksheet_saved", ws, {"year": year})
                messages.success(request, "Worksheet saved.")
                return redirect("tax:worksheet", year=year)
    result = services.compute(ws)
    expenses = Account.objects.filter(type=Account.Type.EXPENSE, is_active=True)
    return render(
        request,
        "tax/worksheet.html",
        {
            "r": result,
            "ws": ws,
            "form": form,
            "can_edit": _can_edit(request.user),
            "income_lines": services.INCOME_LINES,
            "deduction_lines": services.DEDUCTION_LINES,
            "line_labels": services.LINE_LABELS,
            "expenses": expenses,
            "adjust_rows": [
                (n, label, (ws.adjustments or {}).get(str(n), {}))
                for n, label in services.LINE_LABELS.items()
            ],
            "disclaimer": services.DISCLAIMER,
        },
    )


def _line_inputs(post):
    """Parse the per-category line choices, property checkboxes and manual adjustments."""
    valid_ids = set(Account.objects.values_list("pk", flat=True))
    line_map = {}
    for key, value in post.items():
        if key.startswith("line_") and key[5:].isdigit() and is_int(value):
            pk, line = int(key[5:]), int(value)
            if pk in valid_ids and line in services.LINE_LABELS:
                line_map[str(pk)] = line
    nonproperty = sorted(int(v) for v in post.getlist("nonproperty") if is_int(v) and int(v) in valid_ids)
    adjustments, errors = {}, []
    for n in services.LINE_LABELS:
        raw = post.get(f"adj_{n}_cents", "").strip()
        note = post.get(f"adj_{n}_note", "").strip()[:200]
        if not raw:
            continue
        try:
            cents = to_cents(raw)
        except ValueError:
            errors.append(f"Line {n}: '{raw}' isn't an amount.")
            continue
        if abs(cents) > 10**13:
            errors.append(f"Line {n}: that amount is too large.")
            continue
        if cents:
            adjustments[str(n)] = {"cents": cents, "note": note}
    return line_map, nonproperty, adjustments, errors


@board_required
def worksheet_print(request, year):
    _check_year(year)
    result = services.compute(services.get_worksheet(year))
    html = render_to_string(
        "tax/worksheet_print.html",
        {
            "r": result,
            "line_labels": services.LINE_LABELS,
            "income_lines": services.INCOME_LINES,
            "deduction_lines": services.DEDUCTION_LINES,
            "disclaimer": services.DISCLAIMER,
            "generated": timezone.now(),
        },
        request=request,
    )
    if request.GET.get("format") == "pdf":
        response = HttpResponse(render_pdf(html), content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="1120-H-worksheet-{year}.pdf"'
        audit.record(request, "tax.worksheet_exported", None, {"year": year})
        return response
    return HttpResponse(html)


@board_required
def rules(request):
    return render(
        request,
        "tax/rules.html",
        {"configs": TaxYearConfig.objects.select_related("verified_by"), "can_edit": _can_edit(request.user)},
    )


@treasurer_required
def rule_form(request, pk=None):
    config = get_object_or_404(TaxYearConfig, pk=pk) if pk else None
    initial = {}
    if not config:
        latest = TaxYearConfig.objects.first()
        if latest:
            initial = {f: getattr(latest, f) for f in TaxYearConfigForm.Meta.fields}
            initial["year"] = latest.year + 1
            initial["source"] = f"IRS Instructions for Form 1120-H ({latest.year + 1})"
    form = TaxYearConfigForm(request.POST or None, instance=config, initial=initial)
    if request.method == "POST" and form.is_valid():
        config = form.save(commit=False)
        config.verified_on = timezone.localdate()
        config.verified_by = request.user
        config.save()
        audit.record(request, "tax.rules_saved", config, {"year": config.year, "fields": form.changed_data})
        messages.success(request, f"Saved the {config.year} rules.")
        return redirect("tax:rules")
    return render(
        request,
        "tax/rule_form.html",
        {
            "form": form,
            "title": f"{config.year} 1120-H rules" if config else "Add a tax year",
            "back": reverse("tax:rules"),
        },
    )
