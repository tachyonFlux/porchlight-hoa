from django.core.management.base import BaseCommand

from apps.core import setup_token
from apps.core.models import HOASettings


class Command(BaseCommand):
    help = "Print the first-run setup token, if setup hasn't been finished yet."

    def handle(self, *args, **options):
        if HOASettings.load().setup_complete:
            return
        self.stdout.write(
            "\n  Porchlight isn't set up yet. Open the site and enter this setup token:\n\n"
            f"      {setup_token.get()}\n\n"
            "  (It's also saved in the data folder as setup-token.txt.)\n"
        )
