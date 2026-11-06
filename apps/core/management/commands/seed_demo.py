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
        self._seed_meetings(rng, admin)
        self._seed_community_life(admin, created_homes)
        self._seed_compliance_and_documents(admin)
        self._seed_finance(rng, admin)
        self._seed_budget(admin)

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

    def _seed_meetings(self, rng, admin):
        """A past meeting with minutes, an upcoming one, and an open date poll with votes."""
        from datetime import timedelta

        from django.utils import timezone

        from apps.meetings.models import Audience, Meeting, Poll, PollOption, PollVote
        from apps.meetings.services import resident_users

        evening = timezone.localtime().replace(hour=19, minute=0, second=0, microsecond=0)
        Meeting.objects.create(
            title="Annual meeting",
            kind=Meeting.Kind.ANNUAL,
            created_by=admin,
            starts_at=evening - timedelta(days=60),
            ends_at=evening - timedelta(days=60, hours=-2),
            location="Community clubhouse",
            agenda="1. Welcome\n2. Budget review\n3. Board election",
            minutes_text="Quorum present. Budget approved 24-2. Avery Alder re-elected president.",
        )
        Meeting.objects.create(
            title="Board meeting",
            kind=Meeting.Kind.BOARD,
            audience=Audience.BOARD,
            created_by=admin,
            starts_at=evening + timedelta(days=12),
            ends_at=evening + timedelta(days=12, hours=-1),
            virtual_link="https://meet.example.com/lantern-hill",
            agenda="Landscaping bids",
        )
        poll = Poll.objects.create(
            title="Fall neighborhood meeting",
            created_by=admin,
            location="Community clubhouse",
            description="Pick the evenings that work for you. We'll go with the most popular.",
        )
        options = [
            PollOption.objects.create(
                poll=poll,
                starts_at=evening + timedelta(days=d),
                ends_at=evening + timedelta(days=d, hours=-1),
            )
            for d in (21, 23, 28)
        ]
        for user in list(resident_users()[:14]):
            for option in options:
                PollVote.objects.create(
                    option=option, user=user, choice=rng.choice(["yes", "yes", "maybe", "no"])
                )

    def _seed_community_life(self, admin, homes):
        """Announcements, an exterior request in each state, a notice, a Board Room thread."""
        from apps.accounts.models import board_members
        from apps.community.models import Residency
        from apps.exterior import services as exterior
        from apps.exterior.models import ExteriorRequest
        from apps.messaging import services as messaging
        from apps.messaging.models import Announcement, CourtesyNotice
        from apps.messaging.notice_templates import TEMPLATES

        Announcement.objects.create(
            title="Welcome to our new neighborhood site",
            pinned=True,
            created_by=admin,
            body="You can now check your dues, vote on meeting dates, and send exterior requests "
            "all in one place. Questions? Reply to any email from us.",
        )
        Announcement.objects.create(
            title="Spring cleanup day",
            created_by=admin,
            body="Join us at the entrance at 9 AM for mulching and planting. Coffee provided!",
        )
        with_people = [
            r for r in Residency.objects.current().select_related("home", "user") if r.user_id != admin.pk
        ][:4]
        plans = [
            ("Replace back fence", "fence", "6ft cedar privacy fence along the rear lot line.", None),
            ("Paint front door", "paint", "Changing the door from white to a deep navy.", "approved"),
            (
                "Add a garden shed",
                "structure",
                "8x10 shed in the back corner, matching siding.",
                "needs_info",
            ),
        ]
        for r, (title, category, description, outcome) in zip(with_people, plans, strict=False):
            req = exterior.submit(
                r.home, r.user, {"title": title, "category": category, "description": description}, []
            )
            # Board members can't decide requests for their own home.
            decider = next(u for u in board_members() if not exterior.can_act_as_household(u, req))
            if outcome == "approved":
                exterior.board_set_status(
                    req, decider, ExteriorRequest.Status.APPROVED, "Approved. Looks lovely!"
                )
            elif outcome == "needs_info":
                exterior.board_set_status(
                    req,
                    decider,
                    ExteriorRequest.Status.NEEDS_INFO,
                    "Could you share the shed's height and a photo of the spot?",
                )
        if len(with_people) > 3:
            subject, body = TEMPLATES["trash"]
            CourtesyNotice.objects.create(
                home=with_people[3].home, subject=subject, body=body, created_by=admin
            )
        messaging.start_thread(
            admin,
            "Landscaping contract renewal",
            "Our current contract ends in two months. Should we get new bids?",
        )

    def _seed_compliance_and_documents(self, admin):
        """Suggested deadlines and a few small sample PDFs."""
        from django.core.files.base import ContentFile
        from weasyprint import HTML

        from apps.compliance import services as compliance
        from apps.documents.models import Document, DocumentCategory

        compliance.add_suggestions(admin)
        samples = [
            ("Bylaws", "Governing documents", "residents", "Article I. Name. The association shall be..."),
            (
                "Architectural guidelines",
                "Rules & policies",
                "residents",
                "Fences may not exceed six feet...",
            ),
            ("2026 budget", "Budgets & financials", "residents", "Landscaping $18,000; Insurance $4,200..."),
            ("Exterior request form", "Forms", "residents", "Describe your project and attach photos..."),
            ("Insurance policy summary", "Insurance", "board", "General liability coverage of $1,000,000..."),
        ]
        for title, category, visibility, text in samples:
            pdf = HTML(string=f"<h1>{title}</h1><p>Demo document. {text}</p>").write_pdf()
            Document.objects.create(
                title=title,
                category=DocumentCategory.objects.filter(name=category).first(),
                visibility=visibility,
                file=ContentFile(pdf, name=f"{title.lower().replace(' ', '-')}.pdf"),
                original_name=f"{title}.pdf",
                file_size=len(pdf),
                uploaded_by=admin,
            )

    def _seed_finance(self, rng, admin):
        """A checking account with a few months of made-up bank activity and some rules."""
        from datetime import timedelta

        from django.utils import timezone

        from apps.dues.models import Payment
        from apps.finance import services as finance
        from apps.finance.models import Account, BankAccount, CategoryRule
        from apps.finance.parsers import ParsedRow, assign_dedupe_keys

        today = timezone.localdate()
        bank = BankAccount.objects.create(
            name="Operating checking",
            last4="0042",
            opening_balance_cents=1250000,
            opening_date=today.replace(month=1, day=1),
        )
        acct = {a.code: a for a in Account.objects.all()}
        for pattern, code in [
            ("LAWN", "5000"),
            ("BGE", "5100"),
            ("INSURANCE", "5200"),
            ("INTEREST", "4100"),
            ("SERVICE CHARGE", "5900"),
            ("DEPOSIT", "4000"),
        ]:
            CategoryRule.objects.create(pattern=pattern, account=acct[code])
        rows = []
        # Deposit each recorded dues payment a few days after it arrived.
        for payment in Payment.objects.filter(voided_at__isnull=True):
            on = min(payment.received_on + timedelta(days=rng.randint(1, 5)), today)
            rows.append(ParsedRow(line=0, date=on, description="DEPOSIT", amount_cents=payment.amount_cents))
        start = today.replace(month=1, day=1)
        for month in range(1, today.month + 1):
            first = start.replace(month=month)
            rows += [
                ParsedRow(
                    line=0,
                    date=first + timedelta(days=11),
                    description="ACH DEBIT GREENSCAPE LAWN CARE",
                    amount_cents=-rng.choice([85000, 92000]),
                ),
                ParsedRow(
                    line=0,
                    date=first + timedelta(days=17),
                    description="BGE UTILITY PAYMENT",
                    amount_cents=-rng.randint(9000, 16000),
                ),
                ParsedRow(
                    line=0,
                    date=first + timedelta(days=27),
                    description="INTEREST PAYMENT",
                    amount_cents=rng.randint(80, 140),
                ),
            ]
        rows.append(
            ParsedRow(
                line=0,
                date=start + timedelta(days=40),
                description="CHECK 1101 ACME INSURANCE",
                amount_cents=-420000,
                check_number="1101",
            )
        )
        rows = [r for r in rows if r.date <= today]
        assign_dedupe_keys(rows)
        finance.import_rows(rows, bank, "csv", "demo-bank-export.csv", admin)

    def _seed_budget(self, admin):
        """Three past years from a 'spreadsheet', some reserve components and two scenarios."""
        from django.utils import timezone

        from apps.budget import services as budget
        from apps.budget.forms import ScenarioForm
        from apps.budget.models import HistoricalAmount, HistoricalImport, ReserveComponent
        from apps.finance.models import Account

        this_year = timezone.localdate().year
        acct = {a.code: a for a in Account.objects.all()}
        batch = HistoricalImport.objects.create(
            filename="demo-past-years.csv", sha256="demo", created_by=admin
        )
        yearly = {
            "4000": 4200000,
            "4100": 1500,
            "5000": 1020000,
            "5100": 150000,
            "5200": 400000,
            "5300": 180000,
            "5400": 90000,
            "5500": 45000,
            "5700": 120000,
            "5900": 3600,
        }
        # Scale the made-up books to this neighborhood: spending is about 85% of what dues bring in.
        from apps.community.models import Home
        from apps.dues.models import DuesSettings

        dues_total = DuesSettings.load().annual_amount_cents * Home.objects.filter(is_active=True).count()
        spending = sum(v for k, v in yearly.items() if k.startswith("5"))
        scale = (dues_total * 0.85 / spending) if dues_total else 1
        yearly = {k: (dues_total if k == "4000" else int(v * scale)) for k, v in yearly.items()}
        for back, growth in ((3, 0.94), (2, 0.97), (1, 1.0)):
            for code, cents in yearly.items():
                HistoricalAmount.objects.create(
                    batch=batch, year=this_year - back, account=acct[code], amount_cents=int(cents * growth)
                )
        batch.rows = batch.amounts.count()
        batch.save()
        for name, cost, life, left in (
            ("Entrance sign", 600000, 20, 6),
            ("Common-area fence", 1200000, 25, 11),
            ("Stormwater pond maintenance", 900000, 15, 3),
            ("Path repaving", 1500000, 20, 14),
        ):
            ReserveComponent.objects.create(
                name=name,
                replacement_cost_cents=cost,
                useful_life_years=life,
                remaining_life_years=left,
                as_of_year=this_year,
            )
        base = {
            "fiscal_year": this_year + 1,
            "base_year": this_year - 1,
            "inflation_pct": "3",
            "contingency_pct": "5",
            "collection_rate_pct": "97",
            "projection_years": 5,
            "future_increase_pct": "3",
            "starting_operating_cents": "6000",
            "min_operating_balance_cents": "3000",
            "starting_reserve_cents": "15000",
            "round_to_dollar": "on",
            "increase_cap_pct": "10",
        }
        for name, extra in (
            ("Proposed", {}),
            ("Hold the line", {"inflation_pct": "0", "reserve_contribution_cents": "2000"}),
        ):
            form = ScenarioForm(base | {"name": name} | extra)
            if form.is_valid():
                budget.create_scenario(form, admin)
