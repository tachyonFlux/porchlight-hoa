"""Business rules for homes and residents: visibility, claiming, CSV import."""

import csv
import io
import re
from dataclasses import asdict, dataclass, field

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

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


class InviteAlreadyUsedError(Exception):
    """The invite code was used (and rotated) by a simultaneous claim."""


class InactiveUserError(Exception):
    """The email belongs to a deactivated account; the board must reactivate it first."""


def attach_resident(
    home, email, full_name="", phone="", relation=Residency.Relation.OWNER, source=Residency.Source.BOARD
):
    """Get or create the user and give them a current residency at `home`.

    Never overwrites a name or phone the person already set. Returns (user, created_residency).
    Raises InactiveUserError for deactivated accounts.
    """
    email = email.strip().lower()
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        try:
            with transaction.atomic():
                user = User.objects.create_user(email=email, full_name=full_name, phone=phone)
        except IntegrityError:  # created concurrently
            user = User.objects.get(email__iexact=email)
    if not user.is_active:
        raise InactiveUserError(email)
    changed = []
    if full_name and not user.full_name:
        user.full_name = full_name
        changed.append("full_name")
    if phone and not user.phone:
        user.phone = phone
        changed.append("phone")
    if changed:
        user.save(update_fields=changed)
    if Residency.objects.filter(home=home, user=user, ended_on__isnull=True).exists():
        return user, False
    is_first = not home.residencies.current().exists()
    try:
        with transaction.atomic():
            Residency.objects.create(
                home=home, user=user, relation=relation, source=source, is_primary_contact=is_first
            )
    except IntegrityError:  # attached concurrently
        return user, False
    return user, True


def end_residency(residency):
    """End a residency; if it was the primary contact, promote the longest-standing resident."""
    with transaction.atomic():
        residency.ended_on = timezone.localdate()
        was_primary = residency.is_primary_contact
        residency.is_primary_contact = False
        residency.save(update_fields=["ended_on", "is_primary_contact"])
        if was_primary:
            successor = (
                Residency.objects.current().filter(home=residency.home).order_by("started_on", "id").first()
            )
            if successor:
                successor.is_primary_contact = True
                successor.save(update_fields=["is_primary_contact"])


@transaction.atomic
def complete_claim(claim):
    """Apply a confirmed claim token. Returns (user, created_residency)."""
    invite = claim.invite
    if invite.pk:
        # Lock the invite so two simultaneous claims can't both use a single-use code.
        locked = type(invite).objects.select_for_update().get(pk=invite.pk)
        if locked.revoked_at is not None:
            raise InviteAlreadyUsedError(invite.code)
    user, created = attach_resident(
        invite.home,
        claim.email,
        full_name=claim.full_name,
        relation=claim.relation,
        source=Residency.Source.CLAIM,
    )
    invite_model = type(invite)
    invite_model.objects.filter(pk=invite.pk).update(use_count=F("use_count") + 1)
    # Codes are single-use: once someone joins, the home gets a fresh code. Household
    # members can see the new code on their home page to invite others in the household.
    if invite.pk:
        invite_model.objects.filter(pk=invite.pk, revoked_at__isnull=True).update(revoked_at=timezone.now())
        invite_model.active_for(invite.home)
    return user, created


# ---------------------------------------------------------------- CSV import


def _header_target(name):
    """Map a spreadsheet column name to one of our fields, or None."""
    key = re.sub(r"[^a-z0-9#]+", " ", (name or "").lower()).strip()
    words = set(key.split())
    if "mail" in key:  # email, e-mail, Email Address, Owner 1 Email...
        return "email"
    if words & {"phone", "phones", "tel", "telephone", "mobile", "cell"}:
        return "phone"
    if words & {"lot", "lot#"} or key.startswith("lot"):
        return "lot"
    if words & {"address", "street", "property", "home"}:
        return "address"
    if words & {"name", "names", "owner", "owners", "owner(s)", "resident", "residents"}:
        return "names"
    return None


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
        target = _header_target(raw)
        if target and target not in mapping:
            mapping[target] = raw
    if "address" not in mapping:
        raise CSVImportError(
            "Couldn't find an 'address' column. Expected columns: address, lot, owner name(s), email, phone."
        )
    return mapping


def _pair(values, count):
    """Split a cell into `count` parts: by semicolons/newlines, or by commas if that fits."""
    parts = _split(values, r"[;\n]")
    if len(parts) != count:
        by_comma = _split(values, r"[;,\n]")
        if len(by_comma) == count:
            parts = by_comma
    return parts


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
    users = dict(User.objects.values_list("email", "is_active"))
    existing_emails = {e.lower(): active for e, active in users.items()}
    seen_addresses = {}
    rows = []
    warnings = []
    if "email" not in cols:
        warnings.append(
            "No email column was found, so homes will be imported without residents. "
            "Residents can still join with their invite codes."
        )
    for count, record in enumerate(reader, start=1):
        if count > MAX_IMPORT_ROWS:
            raise CSVImportError(f"Too many rows (max {MAX_IMPORT_ROWS}).")

        def get(name, record=record):
            column = cols.get(name)
            return (record.get(column) or "").strip() if column else ""

        if not any((v or "").strip() for v in record.values() if isinstance(v, str)):
            continue  # blank line
        address = normalize_address(get("address"))
        row = ImportRow(line=reader.line_num, address=address, lot=get("lot"))
        if not address:
            row.errors.append("Missing address.")
        elif address.lower() in seen_addresses:
            row.errors.append(f"Duplicate of line {seen_addresses[address.lower()]}.")
        else:
            seen_addresses[address.lower()] = row.line
            row.home_exists = address.lower() in existing_homes

        emails = [e.lower() for e in _split(get("email"), r"[;,\s]+")]
        names = _pair(get("names"), len(emails)) if emails else _split(get("names"), r"[;\n]")
        phones = _pair(get("phone"), len(emails)) if emails else []
        if emails and names and len(names) not in (1, len(emails)):
            row.warnings.append(
                f"{len(names)} names but {len(emails)} emails; names weren't matched to emails."
            )
        for n, email in enumerate(emails):
            try:
                validate_email(email)
            except ValidationError:
                row.errors.append(f"'{email}' isn't a valid email address.")
                continue
            if existing_emails.get(email) is False:
                row.errors.append(f"{email} belongs to a deactivated account.")
                continue
            if len(names) == len(emails):
                name = names[n]
            else:
                name = names[0] if len(names) == 1 else ""
            if len(phones) == len(emails):
                phone = phones[n]
            else:
                phone = phones[0] if n == 0 and phones else ""
            row.people.append(
                ImportPerson(email=email, name=name, phone=phone, exists=email in existing_emails)
            )
        if not emails and names:
            row.warnings.append(
                "No email: the home is imported but names can't be linked until someone claims it."
            )
        rows.append(row)
    return rows, warnings


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
            try:
                _, created = attach_resident(
                    home,
                    person.email,
                    full_name=person.name,
                    phone=person.phone,
                    source=Residency.Source.IMPORT,
                )
            except InactiveUserError:
                continue
            stats["residents_linked"] += int(created)
    return stats
