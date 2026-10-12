"""Small database-backed fixed-window rate limiter.

Uses the main database so limits are shared between Gunicorn workers without Redis.
"""

import functools
import hashlib
import time

from django.db import IntegrityError, transaction
from django.db.models import F
from django.shortcuts import render

from .models import RateLimitHit
from .utils import client_ip


def hit(scope, key, limit, period):
    """Count one attempt. Return True if it is within the limit."""
    window = int(time.time() // period) * period
    digest = hashlib.sha256(f"{scope}:{key}".encode()).hexdigest()
    for _ in range(2):
        try:
            with transaction.atomic():
                obj, _ = RateLimitHit.objects.get_or_create(key=digest, window_start=window)
                RateLimitHit.objects.filter(pk=obj.pk).update(count=F("count") + 1)
                obj.refresh_from_db(fields=["count"])
            return obj.count <= limit
        except IntegrityError:
            continue
    return False


def cleanup(older_than_seconds=2 * 86400):
    cutoff = int(time.time()) - older_than_seconds
    return RateLimitHit.objects.filter(window_start__lt=cutoff).delete()[0]


def too_many(request):
    return render(request, "core/rate_limited.html", status=429)


def rate_limit(scope, limit, period, methods=("POST",)):
    """Decorator limiting a view per client IP."""

    def decorator(view):
        @functools.wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method in methods and not hit(scope, client_ip(request), limit, period):
                return too_many(request)
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
