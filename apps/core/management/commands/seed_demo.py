"""Create a fake neighborhood for development and screenshots.

All names are invented and all emails use the reserved example.com domain.
"""

import random

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import BoardPosition
from apps.community.models import Home, HomeDetail, HomeInvite, Residency
from apps.core.models import HOASettings

STREETS = ["Lantern Way", "Firefly Court", "Juniper Lane", "Old Mill Road"]
FIRST = [
    "Avery",
    "Jordan",
    "Riley",
    "Casey",
    "Morgan",
    "Quinn",
    "Rowan",
    "Sage",
    "Emerson",
    "Harper",
    "Parker",
    "Reese",
    "Dakota",
    "Skyler",
    "Finley",
    "Hayden",
    "Kendall",
    "Logan",
]
LAST = [
    "Alder",
    "Birch",
    "Calloway",
    "Dunmore",
    "Ellery",
    "Fairbanks",
    "Garnett",
    "Holloway",
    "Ingram",
    "Juniper",
    "Kestrel",
    "Lindqvist",
    "Marlow",
    "Northcott",
    "Okafor",
    "Pemberton",
]
DEMO_PASSWORD = "porchlight-demo"  # noqa: S105 - demo data only


class Command(BaseCommand):
    help = "Create a fake demo neighborhood (refuses to run if homes already exist)."

    def add_arguments(self, parser):
        parser.add_argument("--homes", type=int, default=32)
        parser.add_argument("--seed", type=int, default=7)
        parser.add_argument(
            "--allow-production",
            action="store_true",
            help="Run even when DEBUG is off. Creates an admin with a known demo password!",
        )

    @transaction.atomic
    def handle(self, *args, homes, seed, allow_production, **options):
        if not settings.DEBUG and not allow_production:
            raise CommandError(
                "seed_demo creates known passwords; run it with DEBUG on or pass --allow-production."
            )
        if Home.objects.exists():
            raise CommandError("Homes already exist; seed_demo only runs on an empty database.")
        rng = random.Random(seed)  # noqa: S311 - demo data, not security
        User = get_user_model()

        hoa = HOASettings.load()
        hoa.name = hoa.name or "Lantern Hill Community Association"
        hoa.short_name = hoa.short_name or "Lantern Hill"
        hoa.contact_email = hoa.contact_email or "board@example.com"
        hoa.mailing_address = hoa.mailing_address or (
            "Lantern Hill Community Association\nPO Box 100\nAnytown, MD 20000"
        )
        hoa.setup_complete = True
        hoa.save()

        admin = User.objects.filter(email="board@example.com").first()
        if admin is None:
            admin = User.objects.create_superuser("board@example.com", DEMO_PASSWORD, full_name="Avery Alder")
        BoardPosition.objects.get_or_create(user=admin, title=BoardPosition.Title.PRESIDENT)

        created_homes = []
        for i in range(homes):
            street = STREETS[i % len(STREETS)]
            number = 100 + (i // len(STREETS)) * 2 + (i % 2)
            home = Home.objects.create(address=f"{number} {street}", lot_number=str(i + 1))
            created_homes.append(home)
            HomeInvite.active_for(home)
            if rng.random() < 0.2:
                continue  # some homes haven't been claimed yet
            last = rng.choice(LAST)
            for n in range(rng.choice([1, 1, 2])):
                first = rng.choice(FIRST)
                user = User.objects.create_user(
                    f"{first}.{last}.{i}{n}@example.com".lower(),
                    full_name=f"{first} {last}",
                    phone=f"555-01{rng.randint(10, 99)}",
                )
                Residency.objects.create(
                    home=home,
                    user=user,
                    is_primary_contact=(n == 0),
                    relation=Residency.Relation.OWNER,
                    source=Residency.Source.IMPORT,
                )
            if rng.random() < 0.4:
                HomeDetail.objects.create(
                    home=home,
                    key="Dog's name",
                    value=rng.choice(["Biscuit", "Pepper", "Moose"]),
                    visibility=HomeDetail.Visibility.NEIGHBORS,
                )
            if rng.random() < 0.3:
                HomeDetail.objects.create(
                    home=home,
                    key="Emergency contact",
                    value="555-0100",
                    visibility=HomeDetail.Visibility.BOARD,
                )

        # Give the demo board a treasurer and secretary from the residents.
        residents = list(User.objects.filter(residencies__isnull=False).distinct()[:2])
        for user, title in zip(
            residents, [BoardPosition.Title.TREASURER, BoardPosition.Title.SECRETARY], strict=False
        ):
            BoardPosition.objects.create(user=user, title=title)
        if created_homes:
            Residency.objects.get_or_create(
                home=created_homes[0], user=admin, defaults={"is_primary_contact": False}
            )

        self._seed_dues(rng, admin, created_homes)

        self.stdout.write(
            self.style.SUCCESS(
                f"Demo neighborhood ready: {len(created_homes)} homes.\n"
                f"Board sign-in: board@example.com / {DEMO_PASSWORD} (password login)."
            )
        )

    def _seed_dues(self, rng, admin, homes):
        """This year's annual dues: most homes paid, a few partial or late."""
        from datetime import date, timedelta

        from django.utils import timezone

        from apps.dues import periods as dues_periods
        from apps.dues import services as dues
        from apps.dues.models import DuesSettings

        today = timezone.localdate()
        settings_ = DuesSettings.load()
        settings_.amount_cents = 30000
        settings_.payable_to = "Lantern Hill Community Association"
        settings_.late_fee_type = DuesSettings.LateFeeType.FLAT
        settings_.late_fee_flat_cents = 2500
        settings_.payment_instructions = "Questions about your account? Email board@example.com."
        settings_.billing_start_date = date(today.year + 1, 1, 1)
        settings_.save()

        spec = dues_periods.period_containing(today, "annual", 1, 1)
        period = dues.ensure_period(spec, settings_)
        issued = spec.issue_date(settings_.invoice_lead_days)
        for home in homes:
            dues.create_invoice(home, period, issued)
            roll = rng.random()
            paid_on = min(spec.due_date + timedelta(days=rng.randint(-20, 25)), today)
            if roll < 0.75:
                dues.record_payment(
                    home, 30000, "check", paid_on, check_number=str(rng.randint(1000, 9999)), actor=admin
                )
            elif roll < 0.85:
                dues.record_payment(home, 15000, "cash", paid_on, actor=admin)
        dues.apply_late_fees(today)
