from datetime import timedelta

from .models import ClaimToken


def daily(now):
    deleted, _ = ClaimToken.objects.filter(expires_at__lt=now - timedelta(days=7)).delete()
    return {"expired_claim_links_deleted": deleted}
