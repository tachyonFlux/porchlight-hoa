import segno
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.views import complete_login
from apps.core import audit
from apps.core.email import send_templated
from apps.core.permissions import board_required
from apps.core.ratelimit import rate_limit
from apps.core.utils import absolute_url, client_ip

from . import services
from .forms import (
    AddResidentForm,
    ClaimForm,
    HomeDetailForm,
    HomeForm,
    InviteCodeForm,
    ResidentImportForm,
)
from .models import ClaimToken, Home, HomeDetail, HomeInvite, Residency

User = get_user_model()
IMPORT_SESSION_KEY = "resident_import_rows"


# ------------------------------------------------------------- resident-facing


@login_required
def directory(request):
    if not services.is_resident(request.user):
        raise PermissionDenied
    q = request.GET.get("q", "").strip()
    homes = Home.objects.filter(is_active=True).prefetch_related(
        Prefetch(
            "details",
            queryset=HomeDetail.objects.filter(visibility=HomeDetail.Visibility.NEIGHBORS),
            to_attr="neighbor_details",
        )
    )
    if q:
        homes = homes.filter(Q(address__icontains=q) | Q(lot_number__icontains=q))
    mine = set(Residency.objects.current().filter(user=request.user).values_list("home_id", flat=True))
    return render(request, "community/directory.html", {"homes": homes, "q": q, "mine": mine})


@login_required
def home_detail(request, pk):
    home = get_object_or_404(Home, pk=pk)
    user = request.user
    household = services.is_household_member(user, home)
    if not (user.is_board or household or (home.is_active and services.is_resident(user))):
        raise PermissionDenied
    ctx = {
        "home": home,
        "household": household,
        "details": services.visible_details(user, home),
        "can_add_detail": bool(services.editable_levels(user, home)),
        "editable_ids": {d.pk for d in home.details.all() if services.can_edit_detail(user, d)},
        "show_residents": user.is_board or household,
        "residencies": home.current_residencies(),
    }
    if user.is_board:
        ctx["invite"] = HomeInvite.active_for(home, created_by=user)
        ctx["add_resident_form"] = AddResidentForm()
        ctx["past_residencies"] = home.residencies.exclude(ended_on__isnull=True).select_related("user")
    return render(request, "community/home_detail.html", ctx)


@login_required
def detail_add(request, home_pk):
    home = get_object_or_404(Home, pk=home_pk)
    levels = services.editable_levels(request.user, home)
    if not levels:
        raise PermissionDenied
    form = HomeDetailForm(request.POST or None, home=home, allowed_levels=levels)
    if request.method == "POST" and form.is_valid():
        detail = form.save(commit=False)
        detail.home = home
        detail.updated_by = request.user
        detail.save()
        messages.success(request, "Detail added.")
        return redirect(home)
    return render(request, "community/detail_form.html", {"form": form, "home": home})


@login_required
def detail_edit(request, pk):
    detail = get_object_or_404(HomeDetail.objects.select_related("home"), pk=pk)
    if not services.can_edit_detail(request.user, detail):
        raise PermissionDenied
    home = detail.home
    levels = services.editable_levels(request.user, home)
    form = HomeDetailForm(request.POST or None, instance=detail, home=home, allowed_levels=levels)
    if request.method == "POST":
        if "delete" in request.POST:
            detail.delete()
            messages.success(request, "Detail removed.")
            return redirect(home)
        if form.is_valid():
            detail = form.save(commit=False)
            detail.updated_by = request.user
            detail.save()
            messages.success(request, "Detail saved.")
            return redirect(home)
    return render(request, "community/detail_form.html", {"form": form, "home": home, "detail": detail})


# ------------------------------------------------------------------ claim flow


@rate_limit("claim-code", limit=20, period=3600)
def claim_start(request):
    form = InviteCodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        invite = HomeInvite.lookup(form.cleaned_data["code"])
        if invite:
            return redirect("community:claim_code", code=invite.code)
        form.add_error("code", "We couldn't find that code. Check the letter and try again.")
    return render(request, "community/claim_start.html", {"form": form})


@rate_limit("claim-code", limit=20, period=3600, methods=("GET", "POST"))
def claim_code(request, code):
    invite = HomeInvite.lookup(code)
    if invite is None:
        return render(request, "community/claim_invalid.html", status=404)
    home = invite.home

    # Already signed in: their email is verified, so attach right away.
    if request.user.is_authenticated:
        if request.method == "POST":
            relation = request.POST.get("relation", Residency.Relation.OWNER)
            if relation not in Residency.Relation.values:
                relation = Residency.Relation.OWNER
            claim = ClaimToken(invite=invite, email=request.user.email, relation=relation)
            _, created = services.complete_claim(claim)
            audit.record(request, "home.claimed", home, {"email": request.user.email})
            if created:
                messages.success(request, f"Welcome! You're now linked to {home}.")
            return redirect(home)
        return render(
            request,
            "community/claim_signed_in.html",
            {"invite": invite, "home": home, "relations": Residency.Relation.choices},
        )

    form = ClaimForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        _, raw = ClaimToken.issue(
            invite, data["email"], data["full_name"], data["relation"], ip=client_ip(request)
        )
        send_templated(
            data["email"],
            "community/email/claim_confirm",
            {
                "name": data["full_name"],
                "home": home,
                "link": absolute_url(reverse("community:claim_confirm", args=[raw])),
            },
        )
        request.session["claim_email"] = data["email"]
        return redirect("community:claim_sent")
    return render(request, "community/claim_form.html", {"form": form, "home": home})


def claim_sent(request):
    return render(request, "community/claim_sent.html", {"email": request.session.get("claim_email", "")})


@rate_limit("claim-confirm", limit=30, period=3600, methods=("GET", "POST"))
def claim_confirm(request, token):
    claim = ClaimToken.find_valid(token)
    if claim is None:
        return render(request, "community/claim_invalid.html", status=410)
    home = claim.invite.home
    if request.method == "POST":
        if not claim.consume():
            return render(request, "community/claim_invalid.html", status=410)
        user, _ = services.complete_claim(claim)
        audit.record(user, "home.claimed", home, {"email": user.email})
        messages.success(request, f"Welcome! You're now linked to {home}.")
        return complete_login(request, user, home.get_absolute_url())
    return render(request, "community/claim_confirm.html", {"claim": claim, "home": home})


# ------------------------------------------------------------------ board views


@board_required
def board_homes(request):
    q = request.GET.get("q", "").strip()
    current = Q(residencies__ended_on__isnull=True, residencies__user__is_active=True)
    homes = Home.objects.annotate(resident_count=Count("residencies", filter=current))
    if q:
        homes = homes.filter(
            Q(address__icontains=q)
            | Q(lot_number__icontains=q)
            | Q(residencies__user__full_name__icontains=q, residencies__ended_on__isnull=True)
            | Q(residencies__user__email__icontains=q, residencies__ended_on__isnull=True)
        ).distinct()
    show = request.GET.get("show", "")
    if show == "unclaimed":
        homes = homes.filter(resident_count=0, is_active=True)
    elif show == "inactive":
        homes = homes.filter(is_active=False)
    return render(request, "community/board_homes.html", {"homes": homes, "q": q, "show": show})


@board_required
def home_create(request):
    form = HomeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        home = form.save()
        audit.record(request, "home.created", home)
        messages.success(request, f"Added {home}.")
        return redirect(home)
    return render(request, "community/home_form.html", {"form": form})


@board_required
def home_edit(request, pk):
    home = get_object_or_404(Home, pk=pk)
    form = HomeForm(request.POST or None, instance=home)
    if request.method == "POST" and form.is_valid():
        changed = {f: str(form.cleaned_data[f]) for f in form.changed_data}
        form.save()
        audit.record(request, "home.updated", home, changed)
        messages.success(request, "Home saved.")
        return redirect(home)
    return render(request, "community/home_form.html", {"form": form, "home": home})


@board_required
@require_POST
def resident_add(request, pk):
    home = get_object_or_404(Home, pk=pk)
    form = AddResidentForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        user, created = services.attach_resident(home, d["email"], d["full_name"], d["phone"], d["relation"])
        audit.record(request, "home.resident_added", home, {"email": user.email})
        if created:
            messages.success(request, f"{user} added to {home}.")
        else:
            messages.info(request, f"{user} is already linked to {home}.")
    else:
        messages.error(request, "Please enter a valid email address.")
    return redirect(home)


@board_required
@require_POST
def residency_end(request, pk):
    residency = get_object_or_404(Residency.objects.select_related("home", "user"), pk=pk)
    if residency.ended_on is None:
        residency.ended_on = timezone.localdate()
        residency.is_primary_contact = False
        residency.save(update_fields=["ended_on", "is_primary_contact"])
        audit.record(request, "home.resident_removed", residency.home, {"email": residency.user.email})
        messages.success(request, f"{residency.user} is no longer linked to {residency.home}.")
    return redirect(residency.home)


@board_required
@require_POST
def residency_primary(request, pk):
    residency = get_object_or_404(Residency.objects.current(), pk=pk)
    residency.home.residencies.update(is_primary_contact=False)
    Residency.objects.filter(pk=pk).update(is_primary_contact=True)
    messages.success(request, f"{residency.user} is now the primary contact.")
    return redirect(residency.home)


@board_required
@require_POST
def invite_regenerate(request, pk):
    home = get_object_or_404(Home, pk=pk)
    for invite in HomeInvite.objects.valid().filter(home=home):
        invite.revoke()
    invite = HomeInvite.active_for(home, created_by=request.user)
    audit.record(request, "home.invite_regenerated", home)
    messages.success(request, f"New invite code: {invite.display_code}. The old one no longer works.")
    return redirect(home)


@board_required
def invite_letters(request):
    """Printable letters with each home's invite code and QR code."""
    scope = request.GET.get("homes", "unclaimed")
    homes = Home.objects.filter(is_active=True)
    if scope == "unclaimed":
        homes = homes.exclude(residencies__in=Residency.objects.current())
    one = request.GET.get("home")
    if one and one.isdigit():
        homes = Home.objects.filter(pk=int(one))
    letters = []
    for home in homes:
        invite = HomeInvite.active_for(home, created_by=request.user)
        url = absolute_url(invite.get_claim_url())
        letters.append(
            {
                "home": home,
                "invite": invite,
                "url": url,
                "claim_url": absolute_url(reverse("community:claim_start")),
                "qr": segno.make(url, error="m").svg_inline(scale=4, dark="#1f2a36"),
            }
        )
    return render(request, "community/invite_letters.html", {"letters": letters, "scope": scope})


@board_required
def resident_import(request):
    if request.method == "POST" and "confirm" in request.POST:
        data = request.session.pop(IMPORT_SESSION_KEY, None)
        if not data:
            messages.error(request, "That preview expired. Please upload the file again.")
            return redirect("community:import")
        stats = services.apply_import(services.rows_from_session(data))
        audit.record(request, "residents.imported", None, stats)
        messages.success(
            request,
            f"Import done: {stats['homes_created']} homes added, "
            f"{stats['homes_updated']} updated, {stats['residents_linked']} residents linked"
            + (f", {stats['skipped']} rows skipped." if stats["skipped"] else "."),
        )
        return redirect("community:board_homes")

    form = ResidentImportForm(request.POST or None, request.FILES or None)
    rows = None
    if request.method == "POST" and form.is_valid():
        try:
            rows = services.parse_residents_csv(form.cleaned_data["file"])
        except services.CSVImportError as exc:
            form.add_error("file", str(exc))
        else:
            # Only parsed rows go in the session; the uploaded file is never saved.
            request.session[IMPORT_SESSION_KEY] = services.rows_to_session(rows)
    return render(
        request,
        "community/import.html",
        {
            "form": form,
            "rows": rows,
            "ok_count": sum(1 for r in rows if r.ok) if rows else 0,
            "error_count": sum(1 for r in rows if not r.ok) if rows else 0,
        },
    )


@board_required
def people(request):
    q = request.GET.get("q", "").strip()
    users = User.objects.filter(is_active=True).prefetch_related(
        Prefetch(
            "residencies",
            queryset=Residency.objects.current().select_related("home"),
            to_attr="current_residencies",
        )
    )
    if q:
        users = users.filter(Q(full_name__icontains=q) | Q(email__icontains=q))
    return render(request, "community/people.html", {"users": users, "q": q})
