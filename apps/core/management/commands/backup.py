from django.core.management.base import BaseCommand

from apps.core import backup


class Command(BaseCommand):
    help = "Make a consistent copy of the SQLite database in DATA_DIR/backups (e.g. before an upgrade)."

    def handle(self, *args, **options):
        if not backup.is_sqlite():
            self.stdout.write(
                "This server uses PostgreSQL. Back it up with pg_dump, e.g.\n"
                "  docker compose exec db pg_dump -U porchlight porchlight > porchlight.sql"
            )
            return
        # keep=0 here means "don't prune": a manual backup never deletes older ones.
        path = backup.backup_sqlite(keep=0)
        self.stdout.write(self.style.SUCCESS(f"Backed up to {path}"))
