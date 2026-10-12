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
