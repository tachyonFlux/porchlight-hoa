import functools

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect


def _second_factor_ok(request):
    """Users who enabled TOTP must have verified it this session."""
    user = request.user
    return not user.has_totp or user.is_verified()


def _require(check):
    def decorator(view):
        @functools.wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not check(request.user):
                raise PermissionDenied
            if not _second_factor_ok(request):
                return redirect("accounts:otp_verify")
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


board_required = _require(lambda u: u.is_board)
admin_required = _require(lambda u: u.is_admin)


class BoardRequiredMixin:
    def dispatch(self, request, *args, **kwargs):
        return board_required(super().dispatch)(request, *args, **kwargs)


class AdminRequiredMixin:
    def dispatch(self, request, *args, **kwargs):
        return admin_required(super().dispatch)(request, *args, **kwargs)
