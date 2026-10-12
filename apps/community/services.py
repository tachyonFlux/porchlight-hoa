"""Business rules for homes and residents: visibility, claiming, CSV import."""

import csv
import io
import re
from dataclasses import asdict, dataclass, field

from django.contrib.auth import get_user_model
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import F

from .models import Home, HomeDetail, Residency, normalize_address

User = get_user_model()
V = HomeDetail.Visibility


# ---------------------------------------------------------------- visibility


def is_household_member(user, home):
    return user.is_authenticated and Residency.objects.current().filter(user=user, home=home).exists()


def is_resident(user):
    """Anyone currently attached to a home (or on the board) counts as a neighbor."""
    return user.is_authenticated and (user.is_board or Residency.objects.current().filter(user=user).exists())


def visible_levels(user, home):
    """Which detail visibility levels this user may see for this home."""
    if not user.is_authenticated or not user.is_active:
        return set()
    if user.is_board:
        return {V.HOUSEHOLD, V.NEIGHBORS, V.BOARD}
    if is_household_member(user, home):
        return {V.HOUSEHOLD, V.NEIGHBORS}
    if is_resident(user):
        return {V.NEIGHBORS}
    return set()


def visible_details(user, home):
    return home.details.filter(visibility__in=visible_levels(user, home))


def editable_levels(user, home):
    """Board can set any level; a household can manage its own non-board details."""
    if user.is_authenticated and user.is_board:
        return {V.HOUSEHOLD, V.NEIGHBORS, V.BOARD}
    if is_household_member(user, home):
        return {V.HOUSEHOLD, V.NEIGHBORS}
    return set()


def can_edit_detail(user, detail):
    return detail.visibility in editable_levels(user, detail.home)


# ---------------------------------------------------------------- residents


def attach_resident(
    home, email, full_name="", phone="", relation=Residency.Relation.OWNER, source=Residency.Source.BOARD
):
    """Get or create the user and give them a current residency at `home`.

    Never overwrites a name or phone the person already set. Returns (user, created_residency).
    """
    email = email.strip().lower()
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        user = User.objects.create_user(email=email, full_name=full_name, phone=phone)
    else:
        changed = []
        if full_name and not user.full_name:
            user.full_name = full_name
            changed.append("full_name")
        if phone and not user.phone:
            user.phone = phone
            changed.append("phone")
        if changed:
            user.save(update_fields=changed)
    residency = Residency.objects.current().filter(home=home, user=user).first()
    if residency:
        return user, False
    is_first = not home.residencies.current().exists()
    Residency.objects.create(
        home=home, user=user, relation=relation, source=source, is_primary_contact=is_first
    )
    return user, True


@transaction.atomic
def complete_claim(claim):
    """Apply a confirmed claim token. Returns (user, created_residency)."""
    invite = claim.invite
    user, created = attach_resident(
        invite.home,
        claim.email,
        full_name=claim.full_name,
        relation=claim.relation,
        source=Residency.Source.CLAIM,
    )
    type(invite).objects.filter(pk=invite.pk).update(use_count=F("use_count") + 1)
    return user, created


# ---------------------------------------------------------------- CSV import

HEADER_ALIASES = {
    "address": {"address", "street address", "property address", "home", "street"},
    "lot": {"lot", "lot #", "lot number", "lot no", "lot no.", "lot_number"},
    "names": {
        "owner",
        "owners",
        "owner name",
        "owner names",
        "owner name(s)",
        "name",
        "names",
        "owner_names",
    },
    "email": {"email", "emails", "email address", "email addresses", "e-mail"},
    "phone": {"phone", "phones", "phone number", "telephone", "phone numbers"},
}
MAX_IMPORT_ROWS = 2000
MAX_IMPORT_BYTES = 2 * 1024 * 1024


class CSVImportError(Exception):
    pass


@dataclass
class ImportPerson:
    email: str
    name: str = ""
    phone: str = ""
    exists: bool = False


@dataclass
class ImportRow:
    line: int
    address: str
    lot: str = ""
    people: list[ImportPerson] = field(default_factory=list)
    home_exists: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self):
        return not self.errors


def _split(value, pattern=r"[;,\n]"):
    return [part.strip() for part in re.split(pattern, value or "") if part.strip()]


def _map_headers(fieldnames):
    mapping = {}
    for raw in fieldnames or []:
        key = re.sub(r"\s+", " ", (raw or "").strip().lower())
        for target, aliases in HEADER_ALIASES.items():
            if key in aliases and target not in mapping:
                mapping[target] = raw
    if "address" not in mapping:
        raise CSVImportError(
            "Couldn't find an 'address' column. Expected columns: address, lot, owner name(s), email, phone."
        )
    return mapping


def parse_residents_csv(uploaded):
    """Parse an uploaded CSV into ImportRow objects. Nothing is written to the database."""
    raw = uploaded.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        raise CSVImportError("That file is too large (2 MB max).")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    cols = _map_headers(reader.fieldnames)

    existing_homes = {a.lower() for a in Home.objects.values_list("address", flat=True)}
    existing_emails = {e.lower() for e in User.objects.values_list("email", flat=True)}
    seen_addresses = {}
    rows = []
    for i, record in enumerate(reader, start=2):
        if i - 1 > MAX_IMPORT_ROWS:
            raise CSVImportError(f"Too many rows (max {MAX_IMPORT_ROWS}).")

        def get(name, record=record):
            column = cols.get(name)
            return (record.get(column) or "").strip() if column else ""

        if not any((v or "").strip() for v in record.values() if isinstance(v, str)):
            continue  # blank line
        address = normalize_address(get("address"))
        row = ImportRow(line=i, address=address, lot=get("lot"))
        if not address:
            row.errors.append("Missing address.")
        elif address.lower() in seen_addresses:
            row.errors.append(f"Duplicate of line {seen_addresses[address.lower()]}.")
        else:
            seen_addresses[address.lower()] = i
            row.home_exists = address.lower() in existing_homes

        emails = [e.lower() for e in _split(get("email"), r"[;,\s]+")]
        names = _split(get("names"), r"[;\n]")
        phones = _split(get("phone"), r"[;\n]")
        for n, email in enumerate(emails):
            try:
                validate_email(email)
            except Exception:
                row.errors.append(f"'{email}' isn't a valid email address.")
                continue
            if len(names) == len(emails):
                name = names[n]
            else:
                name = names[0] if len(names) == 1 else ""
            phone = phones[n] if len(phones) == len(emails) else (phones[0] if n == 0 and phones else "")
            row.people.append(
                ImportPerson(email=email, name=name, phone=phone, exists=email in existing_emails)
            )
        if not emails and names:
            row.warnings.append(
                "No email: the home is imported but names can't be linked until someone claims it."
            )
        rows.append(row)
    return rows


def rows_to_session(rows):
    return [asdict(r) for r in rows]


def rows_from_session(data):
    rows = []
    for d in data:
        people = [ImportPerson(**p) for p in d.pop("people")]
        rows.append(ImportRow(**d, people=people))
    return rows


@transaction.atomic
def apply_import(rows):
    """Create/update homes and residents for valid rows. Idempotent for repeated imports."""
    stats = {"homes_created": 0, "homes_updated": 0, "residents_linked": 0, "skipped": 0}
    for row in rows:
        if not row.ok:
            stats["skipped"] += 1
            continue
        home = Home.objects.filter(address__iexact=row.address).first()
        if home is None:
            home = Home.objects.create(address=row.address, lot_number=row.lot)
            stats["homes_created"] += 1
        elif row.lot and home.lot_number != row.lot:
            home.lot_number = row.lot
            home.save(update_fields=["lot_number"])
            stats["homes_updated"] += 1
        for person in row.people:
            _, created = attach_resident(
                home,
                person.email,
                full_name=person.name,
                phone=person.phone,
                source=Residency.Source.IMPORT,
            )
            stats["residents_linked"] += int(created)
    return stats
