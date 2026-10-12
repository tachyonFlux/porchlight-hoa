from .models import HOASettings


def porchlight(request):
    user = getattr(request, "user", None)
    return {
        "hoa": HOASettings.load(),
        "is_board": bool(user and user.is_authenticated and user.is_board),
        "is_admin": bool(user and user.is_authenticated and user.is_admin),
    }
