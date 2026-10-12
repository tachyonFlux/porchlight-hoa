"""Exterior change requests: permissions, workflow, notifications."""

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import board_members
from apps.community.services import household_emails, is_household_member
from apps.core import audit
from apps.core.email import send_bulk
from apps.core.utils import absolute_url

from .models import MAX_PHOTOS, ExteriorRequest, RequestEvent, RequestPhoto

S = ExteriorRequest.Status

# Which status changes the board can make from each status.
BOARD_TRANSITIONS = {
    S.SUBMITTED: {S.NEEDS_INFO, S.APPROVED, S.DENIED},
    S.NEEDS_INFO: {S.SUBMITTED, S.APPROVED, S.DENIED},
    S.APPROVED: {S.SUBMITTED},  # reopen
    S.DENIED: {S.SUBMITTED},  # reopen
    S.WITHDRAWN: set(),
}


def can_view(user, req):
    return user.is_authenticated and (user.is_board or is_household_member(user, req.home))


def can_act_as_household(user, req):
    return is_household_member(user, req.home)


def visible_events(user, req):
    events = req.events.select_related("actor")
    return events if user.is_board else events.filter(board_only=False)


def _notify_board(req, template, extra=None, exclude=None):
    emails = board_members().exclude(pk=getattr(exclude, "pk", None)).values_list("email", flat=True)
    send_bulk(emails, template, {"req": req, "url": absolute_url(req.get_absolute_url()), **(extra or {})})


def _notify_household(req, template, extra=None, exclude=None):
    emails = [e for e in household_emails(req.home) if not exclude or e != exclude.email]
    send_bulk(emails, template, {"req": req, "url": absolute_url(req.get_absolute_url()), **(extra or {})})


def add_photos(req, user, files):
    room = MAX_PHOTOS - req.photos.count()
    if len(files) > room:
        raise ValueError(f"You can add {room} more photo{'s' if room != 1 else ''} to this request.")
    return [RequestPhoto.objects.create(request=req, image=f, uploaded_by=user) for f in files]


@transaction.atomic
def submit(home, user, cleaned, photos):
    req = ExteriorRequest.objects.create(
        home=home,
        submitted_by=user,
        title=cleaned["title"],
        category=cleaned["category"],
        description=cleaned["description"],
        planned_start=cleaned.get("planned_start"),
    )
    add_photos(req, user, photos)
    RequestEvent.objects.create(
        request=req, actor=user, kind=RequestEvent.Kind.SUBMITTED, to_status=S.SUBMITTED
    )
    transaction.on_commit(lambda: _notify_board(req, "exterior/email/submitted"))
    return req


@transaction.atomic
def comment(req, user, note, board_only=False, photos=()):
    """Add a comment. A household reply to a 'more info needed' request puts it back in review."""
    board_only = board_only and user.is_board
    event = RequestEvent.objects.create(
        request=req, actor=user, kind=RequestEvent.Kind.COMMENT, note=note, board_only=board_only
    )
    if photos:
        add_photos(req, user, photos)
    # A board member commenting on their own home's request is acting as the household.
    household_reply = can_act_as_household(user, req)
    if household_reply and req.status == S.NEEDS_INFO:
        _change_status(req, user, S.SUBMITTED, "")
    if board_only:
        return event
    if household_reply:
        transaction.on_commit(
            lambda: _notify_board(req, "exterior/email/reply", {"note": note}, exclude=user)
        )
    else:
        transaction.on_commit(lambda: _notify_household(req, "exterior/email/board_comment", {"note": note}))
    return event


def _change_status(req, user, new_status, note):
    old = req.status
    req.status = new_status
    if new_status in ExteriorRequest.DECIDED_STATUSES:
        req.decided_by, req.decided_at = user, timezone.now()
        req.decision_note = note
    elif new_status == S.SUBMITTED:
        req.decided_by, req.decided_at = None, None
    req.save()
    RequestEvent.objects.create(
        request=req,
        actor=user,
        kind=RequestEvent.Kind.STATUS,
        from_status=old,
        to_status=new_status,
        note=note,
    )


@transaction.atomic
def board_set_status(req, user, new_status, note, request=None):
    req = ExteriorRequest.objects.select_for_update().get(pk=req.pk)
    if new_status not in BOARD_TRANSITIONS[S(req.status)]:
        raise ValueError(f"Can't change a request from {req.get_status_display()} to {S(new_status).label}.")
    if new_status == S.NEEDS_INFO and not note.strip():
        raise ValueError("Tell the household what information you need.")
    _change_status(req, user, new_status, note)
    audit.record(request or user, "exterior.status_changed", req, {"status": new_status})
    transaction.on_commit(lambda: _notify_household(req, "exterior/email/status", {"note": note}))
    return req


@transaction.atomic
def withdraw(req, user):
    req = ExteriorRequest.objects.select_for_update().get(pk=req.pk)
    if not req.is_open:
        raise ValueError("Only requests still under review can be withdrawn.")
    _change_status(req, user, S.WITHDRAWN, "")
    transaction.on_commit(lambda: _notify_board(req, "exterior/email/withdrawn", exclude=user))
    return req
