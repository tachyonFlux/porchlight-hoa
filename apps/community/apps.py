from django.apps import AppConfig


class CommunityConfig(AppConfig):
    name = "apps.community"
    label = "community"

    def ready(self):
        from . import signals  # noqa: F401
