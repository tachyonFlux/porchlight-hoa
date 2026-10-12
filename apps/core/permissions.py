import functools

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect


def _second_factor_ok(request):
    """Users who enabled TOTP must have verified it this session."""
    user = request.user
    return not user.has_totp or user.is_verified()


def _must_enroll(request):
    """The HOA can require two-step sign-in for every board member."""
    from .models import HOASettings

    user = request.user
    return user.is_board and not user.has_totp and HOASettings.load().require_board_two_step


def _require(check, enforce_enrollment=True):
    def decorator(view):
        @functools.wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not check(request.user):
                raise PermissionDenied
            if not _second_factor_ok(request):
                return redirect("accounts:otp_verify")
            if enforce_enrollment and _must_enroll(request):
                return redirect("accounts:totp_setup")
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


board_required = _require(lambda u: u.is_board)
admin_required = _require(lambda u: u.is_admin)
# For the pages a board member needs in order to turn two-step sign-in on.
board_required_enrolling = _require(lambda u: u.is_board, enforce_enrollment=False)


class BoardRequiredMixin:
    def dispatch(self, request, *args, **kwargs):
        return board_required(super().dispatch)(request, *args, **kwargs)


class AdminRequiredMixin:
    def dispatch(self, request, *args, **kwargs):
        return admin_required(super().dispatch)(request, *args, **kwargs)
