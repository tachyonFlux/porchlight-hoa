from django.conf import settings


def client_ip(request):
    """Best-effort client IP. Behind a trusted proxy, use the last X-Forwarded-For hop."""
    if settings.BEHIND_PROXY:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.META.get("REMOTE_ADDR") or None


def absolute_url(path):
    return f"{settings.BASE_URL}{path}"
