from django.conf import settings


def client_ip(request):
    """Client IP. Behind N trusted proxies, take the Nth X-Forwarded-For hop from the right.

    Entries further left are client-supplied and can't be trusted, so they're ignored.
    """
    if settings.BEHIND_PROXY:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        count = max(settings.TRUSTED_PROXY_COUNT, 1)
        if len(hops) >= count:
            return hops[-count]
    return request.META.get("REMOTE_ADDR") or None


def absolute_url(path):
    return f"{settings.BASE_URL}{path}"
