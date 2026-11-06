import uuid
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core import audit
from apps.core.money import to_cents
from apps.core.pdf import render_pdf
from apps.core.permissions import board_required, treasurer_required
from apps.finance import parsers
from apps.finance.models import Account

from . import calc, services
from .forms import HistoryMapForm, HistoryUploadForm, OneTimeItemForm, ReserveComponentForm, ScenarioForm
from .models import HistoricalAmount, HistoricalImport, HistoricalMapping, ReserveComponent, Scenario

PENDING_KEY = "budget_pending_history"


def _can_manage(user):
    return user.is_admin or user.holds_title("treasurer")


# ------------------------------------------------------------------ overview


@board_required
def home(request):
    scenarios = Scenario.objects.all()
    years = sorted({s.fiscal_year for s in scenarios}, reverse=True)
    return render(
        request,
        "budget/home.html",
        {
            "groups": [(y, [s for s in scenarios if s.fiscal_year == y]) for y in years],
            "components": ReserveComponent.objects.filter(is_active=True).count(),
            "imports": HistoricalImport.objects.count(),
            "can_manage": _can_manage(request.user),
        },
    )


# ------------------------------------------------------------------ look back


@board_required
def lookback(request):
    data = services.actuals()
    years = data.years[-6:]
    complete = [y for y in services.complete_years(data) if y in years]
    rows = {"income": [], "expense": []}
    totals = {"income": dict.fromkeys(years, 0), "expense": dict.fromkeys(years, 0)}
    for account in sorted(data.accounts, key=lambda a: a.code):
        series = data.series(account.pk)
        kind = "income" if account.type == Account.Type.INCOME else "expense"
        last2 = complete[-2:]
        change = None
        if len(last2) == 2 and series.get(last2[0]):
            change = round((series[last2[1]] - series[last2[0]]) * 100 / series[last2[0]], 1)
        avg = round(sum(series[y] for y in complete) / len(complete)) if complete else None
        rows[kind].append(
            {
                "account": account,
                "cells": [(y, series[y], _drill_url(data, account, y)) for y in years],
                "change": change,
                "average": avg,
            }
        )
        for y in years:
            totals[kind][y] += series[y]
    return render(
        request,
        "budget/lookback.html",
        {
            "years": years,
            "sources": data.sources,
            "partial_year": data.partial_year,
            "rows": rows,
            "totals": {k: [v[y] for y in years] for k, v in totals.items()},
            "net": [totals["income"][y] - totals["expense"][y] for y in years],
            "complete_count": len(complete),
            "change_years": complete[-2:],
        },
    )


def _drill_url(data, account, year):
    if data.sources.get(year) == services.SPREADSHEET:
        return f"{reverse('budget:history')}?year={year}&account={account.pk}"
    return f"{reverse('finance:transactions')}?year={year}&account={account.pk}"


# ------------------------------------------------------------------ historical spreadsheet


@board_required
def history(request):
    qs = HistoricalAmount.objects.select_related("account", "batch")
    f = request.GET
    if f.get("year", "").isdigit():
        qs = qs.filter(year=int(f["year"]))
    if f.get("account", "").isdigit():
        qs = qs.filter(account_id=int(f["account"]))
    if f.get("batch", "").isdigit():
        qs = qs.filter(batch_id=int(f["batch"]))
    if request.method == "POST":
        if not _can_manage(request.user):
            raise Http404
        batch = get_object_or_404(HistoricalImport, pk=request.POST.get("delete_batch") or 0)
        audit.record(request, "budget.history_deleted", batch, {"rows": batch.rows})
        batch.delete()
        messages.success(request, "That import was removed.")
        return redirect("budget:history")
    page = Paginator(qs, 200).get_page(f.get("page"))
    return render(
        request,
        "budget/history.html",
        {
            "page": page,
            "total": qs.aggregate(t=Sum("amount_cents"))["t"] or 0,
            "imports": HistoricalImport.objects.select_related("created_by"),
            "years": HistoricalAmount.objects.values_list("year", flat=True).distinct().order_by("-year"),
            "accounts": Account.objects.filter(type__in=[Account.Type.INCOME, Account.Type.EXPENSE]),
            "f": f,
            "can_manage": _can_manage(request.user),
        },
    )


def _pending_path(token):
    folder = Path(settings.DATA_DIR) / "imports" / "pending"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"hist-{token}.csv"  # cleaned up with the other pending uploads after a day


@treasurer_required
def history_upload(request):
    form = HistoryUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        try:
            text = parsers.decode(upload.read(parsers.MAX_FILE_BYTES + 1))
            services.read_history_csv(text)
        except (parsers.ParseError, services.HistoryError) as exc:
            form.add_error("file", str(exc))
        else:
            token = uuid.uuid4().hex
            _pending_path(token).write_text(text, encoding="utf-8")
            request.session[PENDING_KEY] = {"token": token, "name": upload.name[:200]}
            return redirect("budget:history_map")
    return render(request, "budget/history_upload.html", {"form": form})


@treasurer_required
def history_map(request):
    pending = request.session.get(PENDING_KEY) or {}
    token = str(pending.get("token", ""))
    path = _pending_path(token) if token.isalnum() else None
    if not path or not path.exists():
        messages.error(request, "That upload expired. Please upload the file again.")
        return redirect("budget:history_upload")
    text = path.read_text(encoding="utf-8")
    headers, records = services.read_history_csv(text)
    saved = HistoricalMapping.objects.all()
    chosen = None
    if request.GET.get("mapping", "").isdigit():
        chosen = saved.filter(pk=int(request.GET["mapping"])).first()
    form = HistoryMapForm(
        request.POST or None,
        headers=headers,
        initial_mapping=chosen.columns if chosen else services.guess_history_mapping(headers),
        initial={"amount_sign": chosen.amount_sign if chosen else "positive"},
    )
    accounts = list(
        Account.objects.filter(type__in=[Account.Type.INCOME, Account.Type.EXPENSE], is_active=True)
    )
    by_pk = {a.pk: a for a in accounts}
    rows, error, categories, cat_map = None, None, [], {}
    if form.is_bound and form.is_valid():
        try:
            rows = services.parse_history(headers, records, form.mapping(), form.cleaned_data["default_year"])
        except services.HistoryError as exc:
            error = str(exc)
    if rows is not None:
        categories = sorted({r.category for r in rows if r.category})
        guessed = services.guess_category_map(categories, chosen.category_map if chosen else None)
        for i, cat in enumerate(categories):
            raw = request.POST.get(f"cat_{i}")
            if raw is None:
                cat_map[cat] = by_pk.get(guessed.get(cat))
            else:
                cat_map[cat] = by_pk.get(int(raw)) if raw.isdigit() else None
        duplicate = HistoricalImport.objects.filter(sha256=services.file_hash(text)).first()
        if "confirm" in request.POST:
            if duplicate and not request.POST.get("allow_duplicate"):
                error = f"This exact file was already imported on {duplicate.created_at:%b %d, %Y}."
            else:
                if form.cleaned_data["save_as"]:
                    HistoricalMapping.objects.update_or_create(
                        name=form.cleaned_data["save_as"].strip(),
                        defaults={
                            "columns": form.mapping(),
                            "amount_sign": form.cleaned_data["amount_sign"],
                            "category_map": {k: v.pk for k, v in cat_map.items() if v},
                        },
                    )
                batch = services.import_history(
                    rows,
                    cat_map,
                    form.cleaned_data["amount_sign"],
                    pending.get("name", ""),
                    text,
                    request.user,
                    request,
                )
                path.unlink(missing_ok=True)
                request.session.pop(PENDING_KEY, None)
                skipped = len(rows) - batch.rows
                messages.success(
                    request,
                    f"Imported {batch.rows} amount{'s' if batch.rows != 1 else ''}"
                    + (f"; skipped {skipped} (errors or unmapped categories)." if skipped else "."),
                )
                return redirect(f"{reverse('budget:history')}?batch={batch.pk}")
    else:
        duplicate = None
    preview = []
    if rows:
        for r in rows[:15]:
            preview.append({"row": r, "account": cat_map.get(r.category)})
    return render(
        request,
        "budget/history_map.html",
        {
            "form": form,
            "headers": headers,
            "sample": records[:5],
            "rows": rows,
            "preview": preview,
            "bad": [r for r in rows if r.errors][:20] if rows else [],
            "ok_count": sum(1 for r in rows if not r.errors and cat_map.get(r.category)) if rows else 0,
            "categories": [
                (i, cat, cat_map.get(cat), sum(1 for r in rows if r.category == cat))
                for i, cat in enumerate(categories)
            ],
            "accounts": accounts,
            "error": error,
            "duplicate": duplicate,
            "saved": saved,
            "filename": pending.get("name", ""),
        },
    )


# ------------------------------------------------------------------ reserves


@board_required
def reserves(request):
    year = timezone.localdate().year
    comps = ReserveComponent.objects.all()
    rows = []
    for c in comps:
        k = c.as_calc()
        rows.append(
            {
                "c": c,
                "replacement_year": k.replacement_year,
                "fully_funded": k.fully_funded_cents(year),
                "straight_line": k.straight_line_cents,
                "contribution": k.annual_contribution_cents,
            }
        )
    active = [r for r in rows if r["c"].is_active]
    return render(
        request,
        "budget/reserves.html",
        {
            "rows": rows,
            "year": year,
            "total_cost": sum(r["c"].replacement_cost_cents for r in active),
            "total_fully_funded": sum(r["fully_funded"] for r in active),
            "total_contribution": sum(r["contribution"] for r in active),
            "total_straight_line": sum(r["straight_line"] for r in active),
        },
    )


@board_required
def component_form(request, pk=None):
    component = get_object_or_404(ReserveComponent, pk=pk) if pk else None
    form = ReserveComponentForm(
        request.POST or None,
        instance=component,
        initial={} if component else {"as_of_year": timezone.localdate().year},
    )
    if request.method == "POST":
        if "delete" in request.POST and component:
            audit.record(request, "budget.component_deleted", component)
            component.delete()
            return redirect("budget:reserves")
        if form.is_valid():
            component = form.save()
            audit.record(request, "budget.component_saved", component, {"fields": form.changed_data})
            return redirect("budget:reserves")
    return render(
        request,
        "budget/simple_form.html",
        {
            "form": form,
            "title": "Edit reserve component" if component else "Add a reserve component",
            "back": reverse("budget:reserves"),
            "deletable": component is not None,
        },
    )


# ------------------------------------------------------------------ scenarios


@board_required
def scenario_form(request, pk=None):
    scenario = get_object_or_404(Scenario, pk=pk) if pk else None
    copy_from = None
    if not scenario and request.GET.get("copy", "").isdigit():
        copy_from = get_object_or_404(Scenario, pk=int(request.GET["copy"]))
    if scenario:
        initial = {}
    elif copy_from:
        initial = {f: getattr(copy_from, f) for f in ScenarioForm.Meta.fields}
        initial["name"] = f"{copy_from.name} (copy)"
    else:
        fy = timezone.localdate().year + 1
        op, res = services.suggested_start(fy)
        initial = {
            "name": "Proposed",
            "fiscal_year": fy,
            "base_year": services.default_base_year(fy),
            "starting_operating_cents": op,
            "starting_reserve_cents": res,
        }
    form = ScenarioForm(request.POST or None, instance=scenario, initial=initial)
    if request.method == "POST" and form.is_valid():
        if scenario:
            scenario = form.save()
            audit.record(request, "budget.scenario_saved", scenario, {"fields": form.changed_data})
        else:
            scenario = services.create_scenario(form, request.user, copy_from)
            audit.record(request, "budget.scenario_created", scenario)
        return redirect("budget:scenario", pk=scenario.pk)
    return render(
        request,
        "budget/simple_form.html",
        {
            "form": form,
            "title": f"Edit {scenario.name}" if scenario else "New budget scenario",
            "back": reverse("budget:scenario", args=[scenario.pk]) if scenario else reverse("budget:home"),
            "intro": None
            if scenario
            else "Starting balances are filled in from today's bank balances; adjust them to what you "
            "expect on the first day of the budget year.",
        },
    )


@board_required
def scenario_detail(request, pk):
    scenario = get_object_or_404(Scenario, pk=pk)
    result = services.compute(scenario)
    return render(
        request,
        "budget/scenario.html",
        {
            "s": scenario,
            "r": result,
            "methods": scenario.lines.model.Method.choices,
            "item_form": OneTimeItemForm(scenario=scenario),
            "can_manage": _can_manage(request.user),
            "others": Scenario.objects.filter(fiscal_year=scenario.fiscal_year).exclude(pk=scenario.pk),
        },
    )


@board_required
@require_POST
def scenario_lines(request, pk):
    """Save every line's projection method from the scenario page in one go."""
    scenario = get_object_or_404(Scenario, pk=pk)
    valid = {m for m, _ in scenario.lines.model.Method.choices}
    errors = []
    for line in scenario.lines.select_related("account"):
        method = request.POST.get(f"method_{line.pk}")
        if method not in valid:
            continue
        line.method = method
        years = request.POST.get(f"years_{line.pk}", "")
        if years.isdigit():
            line.years = min(max(int(years), 2), 10)
        raw = request.POST.get(f"manual_{line.pk}", "").strip()
        if raw:
            try:
                line.manual_cents = to_cents(raw)
            except ValueError:
                errors.append(line.account.name)
                continue
        else:
            line.manual_cents = None
        if method == calc.MANUAL and line.manual_cents is None:
            errors.append(line.account.name)
        line.save()
    if errors:
        messages.error(request, "Enter an amount for: " + ", ".join(errors) + ".")
    else:
        messages.success(request, "Projections updated.")
    return redirect(f"{reverse('budget:scenario', args=[scenario.pk])}#lines")


@board_required
@require_POST
def one_time_item(request, pk):
    scenario = get_object_or_404(Scenario, pk=pk)
    if request.POST.get("delete", "").isdigit():
        scenario.one_time_items.filter(pk=int(request.POST["delete"])).delete()
        return redirect(f"{reverse('budget:scenario', args=[scenario.pk])}#one-time")
    form = OneTimeItemForm(request.POST, scenario=scenario)
    if form.is_valid():
        item = form.save(commit=False)
        item.scenario = scenario
        item.save()
    else:
        messages.error(
            request, "Couldn't add that item: " + "; ".join(e for es in form.errors.values() for e in es)
        )
    return redirect(f"{reverse('budget:scenario', args=[scenario.pk])}#one-time")


@board_required
@require_POST
def scenario_delete(request, pk):
    scenario = get_object_or_404(Scenario, pk=pk)
    if scenario.is_adopted or scenario.is_published:
        messages.error(request, "An adopted or published budget can't be deleted.")
        return redirect("budget:scenario", pk=pk)
    audit.record(request, "budget.scenario_deleted", scenario)
    scenario.delete()
    messages.success(request, "Scenario deleted.")
    return redirect("budget:home")


@board_required
def compare(request):
    ids = [int(x) for x in request.GET.getlist("s") if x.isdigit()][:4]
    scenarios = list(Scenario.objects.filter(pk__in=ids)) if ids else []
    if not scenarios:
        fy = request.GET.get("year", "")
        qs = Scenario.objects.all()
        if fy.isdigit():
            qs = qs.filter(fiscal_year=int(fy))
        latest = qs.first()
        scenarios = list(Scenario.objects.filter(fiscal_year=latest.fiscal_year)[:4]) if latest else []
    data = services.actuals()
    results = [services.compute(s, data) for s in scenarios]
    years = sorted({y for r in results for y in r.years})
    return render(
        request,
        "budget/compare.html",
        {
            "results": results,
            "years": years,
            "all": Scenario.objects.all(),
            "chosen": {s.pk for s in scenarios},
        },
    )


def _summary_response(request, summary, filename, draft):
    html = render_to_string(
        "budget/summary_print.html",
        {"b": summary, "generated": timezone.now(), "draft": draft},
        request=request,
    )
    if request.GET.get("format") == "pdf":
        response = HttpResponse(render_pdf(html), content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{filename}.pdf"'
        return response
    return HttpResponse(html)


@board_required
def scenario_print(request, pk):
    scenario = get_object_or_404(Scenario, pk=pk)
    summary = services.summary(services.compute(scenario))
    return _summary_response(
        request, summary, f"budget-{scenario.fiscal_year}", draft=not scenario.is_adopted
    )


@treasurer_required
def scenario_adopt(request, pk):
    from apps.dues.models import BillingPeriod, DuesSettings

    scenario = get_object_or_404(Scenario, pk=pk)
    result = services.compute(scenario)
    if request.method == "POST":
        try:
            services.adopt(scenario, request.user, request)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("budget:scenario", pk=pk)
        messages.success(
            request,
            "Budget adopted. Dues settings now use the new amount for billing periods created from now on.",
        )
        return redirect("budget:scenario", pk=pk)
    today = timezone.localdate()
    return render(
        request,
        "budget/adopt.html",
        {
            "s": scenario,
            "r": result,
            "settings": DuesSettings.load(),
            "existing_periods": BillingPeriod.objects.filter(end__gte=today).order_by("start"),
        },
    )


@treasurer_required
@require_POST
def scenario_publish(request, pk):
    scenario = get_object_or_404(Scenario, pk=pk)
    if request.POST.get("action") == "unpublish":
        services.unpublish(scenario, request.user, request)
        messages.success(request, "Residents can no longer see this budget.")
    else:
        if not scenario.is_adopted:
            messages.error(request, "Adopt the budget before publishing it to residents.")
            return redirect("budget:scenario", pk=pk)
        services.publish(scenario, request.user, request)
        messages.success(request, "Published. Residents can see a copy of this budget as it is now.")
    return redirect("budget:scenario", pk=pk)


# ------------------------------------------------------------------ residents


@login_required
def published_list(request):
    budgets = Scenario.objects.filter(published_snapshot__isnull=False).order_by("-fiscal_year")
    return render(request, "budget/published_list.html", {"budgets": budgets})


@login_required
def published(request, year):
    scenario = Scenario.objects.filter(published_snapshot__isnull=False, fiscal_year=year).first()
    if scenario is None:
        raise Http404
    if request.GET.get("format") in ("pdf", "print"):
        return _summary_response(request, scenario.published_snapshot, f"budget-{year}", draft=False)
    return render(request, "budget/published.html", {"s": scenario, "b": scenario.published_snapshot})
