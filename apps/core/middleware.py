from django.shortcuts import redirect
from django.urls import reverse

from .models import HOASettings

_EXEMPT_PREFIXES = ("/static/", "/setup/", "/login/", "/logout/", "/healthz")


class SetupRequiredMiddleware:
    """Send every request to the setup wizard until the first-run setup is done."""

    def __init__(self, get_response):
        self.get_response = get_response
        self._complete = False

    def __call__(self, request):
        if not self._complete:
            if HOASettings.load().setup_complete:
                self._complete = True
            elif not request.path.startswith(_EXEMPT_PREFIXES):
                return redirect(reverse("core:setup"))
        return self.get_response(request)


_SECOND_FACTOR_EXEMPT = ("/static/", "/login/", "/logout/", "/account/two-step/", "/healthz", "/setup/")


class BoardSecondFactorMiddleware:
    """Board accounts that use two-step sign-in (or are required to) can't use *any* page
    until that's satisfied, not just pages marked board-only. Resident-facing pages also
    show board-level detail to board members, so a password alone must not unlock them."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and not request.path.startswith(_SECOND_FACTOR_EXEMPT)
            and user.is_board
        ):
            from .permissions import _must_enroll, _second_factor_ok

            if not _second_factor_ok(request):
                return redirect("accounts:otp_verify")
            if _must_enroll(request):
                return redirect("accounts:totp_setup")
        return self.get_response(request)
