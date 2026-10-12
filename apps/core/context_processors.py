from .models import HOASettings


def porchlight(request):
    user = getattr(request, "user", None)
    is_board = bool(user and user.is_authenticated and user.is_board)
    board_unread = 0
    if is_board:
        from apps.messaging.services import unread_count

        board_unread = unread_count(user)
    return {
        "hoa": HOASettings.load(),
        "is_board": is_board,
        "board_unread": board_unread,
        "is_admin": bool(user and user.is_authenticated and user.is_admin),
    }
