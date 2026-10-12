from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.accounts.models import BoardPosition
from apps.community.models import Home, Residency

from . import audit, setup_token
from .forms import HOASettingsForm, SetupForm, SetupNameForm
from .models import AuditLog, HOASettings, TaskRun
from .permissions import admin_required, board_required
from .ratelimit import rate_limit


def healthz(request):
    return HttpResponse("ok", content_type="text/plain")


def permission_denied(request, exception=None):
    return render(request, "core/403.html", status=403)


def page_not_found(request, exception=None):
    return render(request, "core/404.html", status=404)


@rate_limit("setup", limit=10, period=900)
def setup(request):
    hoa = HOASettings.load()
    if hoa.setup_complete:
        raise Http404
    User = get_user_model()
    admin_exists = User.objects.filter(is_superuser=True, is_active=True).exists()

    if admin_exists:
        # An admin was created from the command line; they just need to name the HOA.
        if not (request.user.is_authenticated and request.user.is_admin):
            return render(request, "core/setup_login_needed.html")
        form = SetupNameForm(request.POST or None, initial={"hoa_name": hoa.name})
        if request.method == "POST" and form.is_valid():
            hoa.name = form.cleaned_data["hoa_name"]
            hoa.setup_complete = True
            hoa.save()
            audit.record(request, "setup.completed", hoa)
            messages.success(request, "You're all set. Welcome to Porchlight!")
            return redirect("core:dashboard")
        return render(request, "core/setup.html", {"form": form, "admin_exists": True})

    form = SetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            # Re-check under a row lock so two simultaneous submissions can't both win.
            hoa = HOASettings.objects.select_for_update().get(pk=hoa.pk)
            if hoa.setup_complete:
                raise Http404
            user = User.objects.create_superuser(
                email=data["email"], password=data["password1"], full_name=data["full_name"]
            )
            BoardPosition.objects.create(user=user, title=data["title"])
            hoa.name = data["hoa_name"]
            hoa.setup_complete = True
            hoa.save()
            audit.record(user, "setup.completed", hoa)
        setup_token.discard()
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        messages.success(request, "You're all set. Next, add your homes.")
        return redirect("core:dashboard")
    return render(request, "core/setup.html", {"form": form, "admin_exists": False})


@login_required
def dashboard(request):
    user = request.user
    residencies = (
        Residency.objects.current().filter(user=user).select_related("home").order_by("home__address")
    )
    ctx = {"residencies": residencies}
    if user.is_board:
        homes = Home.objects.filter(is_active=True)
        ctx["board_stats"] = {
            "homes": homes.count(),
            "unclaimed": homes.exclude(residencies__in=Residency.objects.current()).count(),
            "residents": get_user_model()
            .objects.filter(residencies__in=Residency.objects.current())
            .distinct()
            .count(),
        }
        ctx["last_task_run"] = TaskRun.objects.first()
    return render(request, "core/dashboard.html", ctx)


@admin_required
def hoa_settings(request):
    hoa = HOASettings.load()
    form = HOASettingsForm(request.POST or None, instance=hoa)
    if request.method == "POST" and form.is_valid():
        changed = {f: str(form.cleaned_data[f]) for f in form.changed_data}
        form.save()
        audit.record(request, "settings.updated", hoa, changed)
        messages.success(request, "Settings saved.")
        return redirect("core:settings")
    return render(request, "core/settings.html", {"form": form})


@board_required
def audit_log(request):
    entries = AuditLog.objects.select_related("actor")[:300]
    return render(request, "core/audit_log.html", {"entries": entries, "now": timezone.now()})
