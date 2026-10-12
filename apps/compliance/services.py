"""Compliance deadlines: completion/recurrence, reminders, suggested defaults."""

from datetime import date

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import board_members
from apps.core import audit
from apps.core.email import send_bulk
from apps.core.utils import absolute_url

from .models import Deadline, DeadlineReminder, add_years


@transaction.atomic
def complete(deadline, user, note="", request=None):
    """Mark done; a yearly deadline gets next year's copy (once)."""
    deadline = Deadline.objects.select_for_update().get(pk=deadline.pk)
    if deadline.is_done:
        return deadline.next_occurrence
    deadline.completed_at = timezone.now()
    deadline.completed_by = user
    deadline.completion_note = note
    nxt = None
    if deadline.recurrence == Deadline.Recurrence.YEARLY:
        nxt = Deadline.objects.create(
            title=deadline.title,
            category=deadline.category,
            description=deadline.description,
            due_date=add_years(deadline.due_date, 1),
            recurrence=deadline.recurrence,
            remind_days=deadline.remind_days,
            owner=deadline.owner,
        )
        deadline.next_occurrence = nxt
    deadline.save()
    audit.record(request or user, "compliance.completed", deadline, {"note": note})
    return nxt


@transaction.atomic
def reopen(deadline, user, request=None):
    """Undo a completion. Removes the auto-created next occurrence if it's untouched."""
    deadline = Deadline.objects.select_for_update().get(pk=deadline.pk)
    nxt = deadline.next_occurrence
    deadline.completed_at = None
    deadline.completed_by = None
    deadline.completion_note = ""
    deadline.next_occurrence = None
    deadline.save()
    if nxt and not nxt.is_done and not nxt.reminders.exists():
        nxt.delete()
    audit.record(request or user, "compliance.reopened", deadline)


def _recipients(deadline):
    if deadline.owner and deadline.owner.is_active:
        return [deadline.owner.email]
    return list(board_members().values_list("email", flat=True))


def _send(deadline, days_before, template):
    try:
        with transaction.atomic():
            DeadlineReminder.objects.create(deadline=deadline, days_before=days_before)
    except IntegrityError:
        return False
    send_bulk(
        _recipients(deadline),
        template,
        {"deadline": deadline, "url": absolute_url(reverse("compliance:list"))},
    )
    return True


def send_reminders(today):
    """For each open deadline send at most one reminder a day: the closest threshold
    reached that hasn't been sent, plus a single 'overdue' alert. Idempotent."""
    sent = {"reminders": 0, "overdue": 0}
    for deadline in Deadline.objects.filter(completed_at__isnull=True).select_related("owner"):
        days_left = (deadline.due_date - today).days
        already = set(deadline.reminders.values_list("days_before", flat=True))
        if days_left < 0:
            if DeadlineReminder.OVERDUE not in already and _send(
                deadline, DeadlineReminder.OVERDUE, "compliance/email/overdue"
            ):
                sent["overdue"] += 1
            continue
        due = [d for d in deadline.reminder_days if d >= days_left and d not in already]
        if not due:
            continue
        closest = min(due)
        # Thresholds we skipped over (e.g. the deadline was added late) count as handled.
        for skipped in due:
            if skipped != closest:
                DeadlineReminder.objects.get_or_create(deadline=deadline, days_before=skipped)
        if _send(deadline, closest, "compliance/email/reminder"):
            sent["reminders"] += 1
    return sent


# ------------------------------------------------------------------ suggestions

# Starting points only. Due dates and requirements vary by association and change over
# time, so each description says to confirm them. Nothing here is legal or tax advice.
MARYLAND_SUGGESTIONS = [
    {
        "title": "Maryland annual report / personal property return (SDAT)",
        "category": Deadline.Category.FILING,
        "month_day": (4, 15),
        "description": "Many Maryland entities, including incorporated associations, file an annual "
        "report with the State Department of Assessments and Taxation (SDAT). Confirm whether your "
        "association must file, which form applies, and the current due date on the SDAT website, "
        "then adjust this date.",
    },
    {
        "title": "Federal income tax return (Form 1120-H or 1120)",
        "category": Deadline.Category.TAX,
        "month_day": (4, 15),
        "description": "HOAs generally file a federal return each year. For a calendar tax year the "
        "due date is typically in mid-April. Confirm the form, your fiscal year end, and the date "
        "with your accountant or the current IRS instructions.",
    },
    {
        "title": "Maryland state tax return (ask your accountant)",
        "category": Deadline.Category.TAX,
        "month_day": (4, 15),
        "description": "Ask your accountant whether the association needs to file a Maryland return "
        "and when. Delete this item if it doesn't apply.",
    },
    {
        "title": "Insurance renewal: property, general liability, D&O",
        "category": Deadline.Category.INSURANCE,
        "month_day": None,
        "description": "Set this to your policies' renewal date. Review coverage amounts and get "
        "quotes a month or two ahead.",
    },
    {
        "title": "Annual meeting",
        "category": Deadline.Category.GOVERNANCE,
        "month_day": None,
        "description": "Check your bylaws for when the annual meeting must be held and how much "
        "notice owners must receive, then set this date.",
    },
    {
        "title": "Adopt and distribute next year's budget",
        "category": Deadline.Category.FINANCE,
        "month_day": (11, 1),
        "description": "Check your bylaws and the Maryland Homeowners Association Act for when the "
        "proposed budget must be prepared and shared with owners, then adjust this date.",
    },
    {
        "title": "Review resale disclosure packet",
        "category": Deadline.Category.GOVERNANCE,
        "month_day": (1, 31),
        "description": "Maryland requires certain disclosures when a home in an HOA is sold. Once a "
        "year, check that your governing documents, budget, and fee information are current so "
        "sellers can get them quickly. Confirm requirements with counsel.",
    },
]


def _next_date(month_day, today):
    if month_day is None:
        return add_years(today, 1)
    month, day = month_day
    candidate = date(today.year, month, day)
    return candidate if candidate >= today else add_years(candidate, 1)


def add_suggestions(user, today=None, request=None):
    """Create the suggested deadlines that don't exist yet (matched by title)."""
    today = today or timezone.localdate()
    existing = set(Deadline.objects.values_list("title", flat=True))
    created = []
    for item in MARYLAND_SUGGESTIONS:
        if item["title"] in existing:
            continue
        created.append(
            Deadline.objects.create(
                title=item["title"],
                category=item["category"],
                description=item["description"],
                due_date=_next_date(item["month_day"], today),
            )
        )
    if created:
        audit.record(request or user, "compliance.suggestions_added", None, {"count": len(created)})
    return created
