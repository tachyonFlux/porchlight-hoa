from datetime import timedelta

from .models import MagicLinkToken


def daily(now):
    """Drop sign-in links that expired more than a week ago."""
    deleted, _ = MagicLinkToken.objects.filter(expires_at__lt=now - timedelta(days=7)).delete()
    return {"expired_magic_links_deleted": deleted}
