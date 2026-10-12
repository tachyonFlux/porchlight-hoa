from base64 import b32encode
from datetime import datetime, timedelta
from urllib.parse import urlencode

import segno
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.views import PasswordChangeView
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django_otp import login as otp_login
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.core import audit
from apps.core.email import send_templated
from apps.core.permissions import admin_required, board_required, board_required_enrolling
from apps.core.ratelimit import hit, rate_limit, too_many
from apps.core.utils import absolute_url, client_ip

from .forms import BoardPositionForm, EmailForm, OTPForm, PasswordLoginForm, ProfileForm
from .models import BoardPosition, MagicLinkToken

User = get_user_model()

PENDING_USER_KEY = "porchlight_pending_user"
PENDING_NEXT_KEY = "porchlight_pending_next"
PENDING_TTL = timedelta(minutes=10)
BACKEND = "django.contrib.auth.backends.ModelBackend"


def _safe_next(request, value):
    if value and url_has_allowed_host_and_scheme(
        value, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return value
    return reverse(settings.LOGIN_REDIRECT_URL)


def complete_login(request, user, next_url=None):
    """Log the user in, or hand off to the TOTP step if they have it enabled."""
    next_url = _safe_next(request, next_url)
    if user.has_totp:
        request.session[PENDING_USER_KEY] = {"id": user.pk, "at": timezone.now().isoformat()}
        request.session[PENDING_NEXT_KEY] = next_url
        return redirect("accounts:otp_verify")
    login(request, user, backend=BACKEND)
    return redirect(next_url)


def send_magic_link(request, user, next_url=""):
    _, raw = MagicLinkToken.issue(user, ip=client_ip(request))
    path = reverse("accounts:magic_link", args=[raw])
    if next_url:
        path += "?" + urlencode({"next": next_url})
    send_templated(
        user.email,
        "accounts/email/magic_link",
        {"user": user, "link": absolute_url(path), "ttl": settings.MAGIC_LINK_TTL_MINUTES},
        background=True,
    )


@rate_limit("login-request", limit=10, period=3600)
def login_request(request):
    if request.user.is_authenticated:
        return redirect("core:dashboard")
    next_url = request.GET.get("next", "") or request.POST.get("next", "")
    form = EmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        # Limit per address too, so nobody can flood one inbox from many IPs.
        if not hit("login-email", email, limit=5, period=3600):
            return too_many(request)
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user:
            send_magic_link(request, user, next_url if next_url.startswith("/") else "")
        # Same response either way so the form can't be used to discover who lives here.
        request.session["magic_link_email"] = email
        return redirect("accounts:login_sent")
    return render(request, "accounts/login.html", {"form": form, "next": next_url})


def login_sent(request):
    return render(request, "accounts/login_sent.html", {"email": request.session.get("magic_link_email", "")})


@rate_limit("magic-link", limit=30, period=3600, methods=("GET", "POST"))
def magic_link(request, token):
    link = MagicLinkToken.find_valid(token)
    if link is None:
        return render(request, "accounts/link_invalid.html", status=410)
    # A confirm step (POST) keeps email link scanners from burning the token.
    if request.method == "POST":
        if not link.consume():
            return render(request, "accounts/link_invalid.html", status=410)
        if request.user.is_authenticated and request.user != link.user:
            logout(request)
        return complete_login(request, link.user, request.GET.get("next"))
    return render(request, "accounts/link_confirm.html", {"link_user": link.user})


@rate_limit("password-login", limit=10, period=900)
def password_login(request):
    if request.user.is_authenticated:
        return redirect("core:dashboard")
    form = PasswordLoginForm(request.POST or None)
    error = None
    if request.method == "POST" and form.is_valid():
        # Also limit per account so a password can't be guessed from many IPs.
        if not hit("password-login-email", form.cleaned_data["email"].lower(), limit=20, period=3600):
            return too_many(request)
        user = authenticate(request, email=form.cleaned_data["email"], password=form.cleaned_data["password"])
        if user is not None:
            return complete_login(request, user, request.GET.get("next"))
        error = "That email and password don't match."
    return render(request, "accounts/password_login.html", {"form": form, "error": error})


def _pending_user(request):
    pending = request.session.get(PENDING_USER_KEY)
    if not pending:
        return None
    started = datetime.fromisoformat(pending["at"])
    if timezone.now() - started > PENDING_TTL:
        request.session.pop(PENDING_USER_KEY, None)
        return None
    return User.objects.filter(pk=pending["id"], is_active=True).first()


@rate_limit("otp-verify", limit=10, period=900)
def otp_verify(request):
    if request.user.is_authenticated:
        user = request.user
        if not user.has_totp or user.is_verified():
            return redirect("core:dashboard")
    else:
        user = _pending_user(request)
        if user is None:
            messages.info(request, "Please sign in again.")
            return redirect("accounts:login")

    form = OTPForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        token = form.cleaned_data["token"]
        device = next(
            (d for d in TOTPDevice.objects.devices_for_user(user, confirmed=True) if d.verify_token(token)),
            None,
        )
        if device:
            next_url = request.session.pop(PENDING_NEXT_KEY, None)
            request.session.pop(PENDING_USER_KEY, None)
            if not request.user.is_authenticated:
                login(request, user, backend=BACKEND)
            otp_login(request, device)
            return redirect(_safe_next(request, next_url))
        form.add_error("token", "That code didn't work. Codes change every 30 seconds.")
    return render(request, "accounts/otp_verify.html", {"form": form})


@require_POST
def logout_view(request):
    logout(request)
    messages.info(request, "You've been signed out.")
    return redirect("accounts:login")


@login_required
def account(request):
    form = ProfileForm(request.POST or None, instance=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Profile saved.")
        return redirect("accounts:account")
    return render(
        request,
        "accounts/account.html",
        {"form": form, "has_password": request.user.has_usable_password()},
    )


class BoardPasswordChangeView(PasswordChangeView):
    template_name = "accounts/password_change.html"
    success_url = reverse_lazy("accounts:account")

    def get_form_class(self):
        # First-time board passwords don't have an "old password" to confirm.
        if not self.request.user.has_usable_password():
            return SetPasswordForm
        return super().get_form_class()

    def form_valid(self, form):
        messages.success(self.request, "Password saved.")
        audit.record(self.request, "account.password_set", self.request.user)
        return super().form_valid(form)


password_change = board_required_enrolling(BoardPasswordChangeView.as_view())


def _totp_qr_svg(device):
    return segno.make(device.config_url, error="m").svg_inline(scale=5, dark="#1f2a36")


@board_required_enrolling
def totp_setup(request):
    user = request.user
    if user.has_totp:
        return render(request, "accounts/totp_enabled.html")
    device = TOTPDevice.objects.filter(user=user, confirmed=False).first()
    if device is None:
        device = TOTPDevice.objects.create(user=user, name="Authenticator app", confirmed=False)
    form = OTPForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if device.verify_token(form.cleaned_data["token"]):
            device.confirmed = True
            device.save(update_fields=["confirmed"])
            otp_login(request, device)
            audit.record(request, "account.totp_enabled", user)
            messages.success(request, "Two-step sign-in is on.")
            return redirect("accounts:account")
        form.add_error("token", "That code didn't work. Check your phone's clock and try again.")
    secret = b32encode(device.bin_key).decode()
    return render(
        request,
        "accounts/totp_setup.html",
        {"form": form, "qr_svg": _totp_qr_svg(device), "secret": secret},
    )


@board_required
def totp_disable(request):
    form = OTPForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        devices = list(TOTPDevice.objects.devices_for_user(request.user, confirmed=True))
        if any(d.verify_token(form.cleaned_data["token"]) for d in devices):
            TOTPDevice.objects.filter(user=request.user).delete()
            audit.record(request, "account.totp_disabled", request.user)
            messages.success(request, "Two-step sign-in is off.")
            return redirect("accounts:account")
        form.add_error("token", "That code didn't work.")
    return render(request, "accounts/totp_disable.html", {"form": form})


@board_required
def board_list(request):
    positions = BoardPosition.objects.select_related("user")
    current = [p for p in positions if p.is_current]
    past = [p for p in positions if not p.is_current]
    return render(request, "accounts/board_list.html", {"current": current, "past": past})


@admin_required
def board_add(request):
    form = BoardPositionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].strip().lower()
        with transaction.atomic():
            user = User.objects.filter(email__iexact=email).first()
            if user is None:
                user = User.objects.create_user(email=email, full_name=form.cleaned_data["full_name"])
            position = form.save(commit=False)
            position.user = user
            position.save()
            audit.record(
                request,
                "board.position_added",
                position,
                {"user": user.email, "title": position.display_title},
            )
        messages.success(request, f"{user} added as {position.display_title}.")
        return redirect("accounts:board_list")
    return render(request, "accounts/board_form.html", {"form": form})


@admin_required
@require_POST
def board_end(request, pk):
    position = get_object_or_404(BoardPosition, pk=pk, ended_on__isnull=True)
    position.ended_on = timezone.localdate()
    position.save(update_fields=["ended_on"])
    audit.record(request, "board.position_ended", position, {"user": position.user.email})
    messages.success(request, f"{position} ended today.")
    return redirect("accounts:board_list")
