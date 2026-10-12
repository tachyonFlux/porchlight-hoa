from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import board_members
from apps.community.services import household_emails, is_household_member
from apps.core import audit
from apps.core.email import send_bulk
from apps.core.ratelimit import hit
from apps.core.utils import absolute_url
from apps.meetings.services import audience_users, can_see

from .models import Announcement, BoardMessage, BoardThread, BoardThreadRead, CourtesyNotice, NoticeReply

# ------------------------------------------------------------------ announcements


def visible_announcements(user):
    qs = Announcement.objects.select_related("created_by")
    if user.is_board:
        return qs
    if can_see(user, "residents"):
        return qs.filter(audience=Announcement.Audience.RESIDENTS)
    return qs.none()


def email_announcement(announcement, request=None):
    emails = audience_users(announcement.audience).values_list("email", flat=True)
    count = send_bulk(
        emails,
        "messaging/email/announcement",
        {"announcement": announcement, "url": absolute_url(announcement.get_absolute_url())},
    )
    Announcement.objects.filter(pk=announcement.pk).update(emailed_at=timezone.now(), email_count=count)
    audit.record(request, "messaging.announcement_emailed", announcement, {"recipients": count})
    return count


# ------------------------------------------------------------------ courtesy notices


def can_view_notice(user, notice):
    return user.is_authenticated and (user.is_board or is_household_member(user, notice.home))


def send_notice(notice, request=None):
    emails = household_emails(notice.home)
    send_bulk(
        emails, "messaging/email/notice", {"notice": notice, "url": absolute_url(notice.get_absolute_url())}
    )
    CourtesyNotice.objects.filter(pk=notice.pk).update(emailed_at=timezone.now())
    return len(emails)


NOTICE_REPLIES_PER_HOUR = 20
NOTICES_PER_HOME_PER_DAY = 3
BOARD_ROOM_EMAILS_PER_DAY = 30


@transaction.atomic
def reply_to_notice(notice, user, body):
    """Household replies email the board; board replies email the household."""
    if not hit("notice-reply", user.pk, limit=NOTICE_REPLIES_PER_HOUR, period=3600):
        raise ValueError("That's a lot of replies in a short time. Please try again later.")
    reply = NoticeReply.objects.create(notice=notice, author=user, body=body)
    ctx = {"notice": notice, "reply": reply, "url": absolute_url(notice.get_absolute_url())}
    if is_household_member(user, notice.home):
        emails = list(board_members().exclude(pk=user.pk).values_list("email", flat=True))
        template = "messaging/email/notice_reply"
    else:
        emails = [e for e in household_emails(notice.home) if e != user.email]
        template = "messaging/email/notice_board_reply"
    transaction.on_commit(lambda: send_bulk(emails, template, ctx))
    return reply


def notice_email_allowed(home):
    """At most 3 courtesy-notice emails to one home per day, whoever sends them."""
    return hit("notice-home", home.pk, limit=NOTICES_PER_HOME_PER_DAY, period=86400)


@transaction.atomic
def set_notice_status(notice, user, status, note="", request=None):
    """Resolve or reopen. Idempotent: asking for the current status changes nothing."""
    notice = CourtesyNotice.objects.select_for_update().get(pk=notice.pk)
    if notice.status == status:
        return notice
    notice.status = status
    if status == CourtesyNotice.Status.RESOLVED:
        notice.resolved_at, notice.resolved_by, notice.resolution_note = timezone.now(), user, note
    else:
        notice.resolved_at, notice.resolved_by = None, None
    notice.save()
    audit.record(request or user, f"messaging.notice_{status}", notice)
    return notice


# ------------------------------------------------------------------ board room


def threads_with_unread(user):
    threads = list(BoardThread.objects.select_related("created_by"))
    reads = dict(BoardThreadRead.objects.filter(user=user).values_list("thread_id", "last_read_at"))
    for thread in threads:
        last = reads.get(thread.pk)
        thread.unread = last is None or thread.last_activity_at > last
    return threads


def mark_read(thread, user, seen_up_to=None):
    """Record what the user has seen: the newest message actually shown to them, so a
    message committed while the page was rendering still counts as unread."""
    seen_up_to = seen_up_to or timezone.now()
    try:
        with transaction.atomic():
            marker, created = BoardThreadRead.objects.get_or_create(
                thread=thread, user=user, defaults={"last_read_at": seen_up_to}
            )
    except IntegrityError:  # two tabs opening the thread at once
        marker, created = BoardThreadRead.objects.get(thread=thread, user=user), False
    if not created and marker.last_read_at < seen_up_to:
        BoardThreadRead.objects.filter(pk=marker.pk).update(last_read_at=seen_up_to)


def unread_count(user):
    reads = dict(BoardThreadRead.objects.filter(user=user).values_list("thread_id", "last_read_at"))
    return sum(
        1
        for pk, activity in BoardThread.objects.values_list("pk", "last_activity_at")
        if pk not in reads or activity > reads[pk]
    )


@transaction.atomic
def post_message(thread, user, body, notify=False):
    message = BoardMessage.objects.create(thread=thread, author=user, body=body)
    BoardThread.objects.filter(pk=thread.pk).update(last_activity_at=message.created_at)
    mark_read(thread, user, message.created_at)
    if notify and hit("board-room-email", user.pk, limit=BOARD_ROOM_EMAILS_PER_DAY, period=86400):
        emails = board_members().exclude(pk=user.pk).values_list("email", flat=True)
        transaction.on_commit(
            lambda: send_bulk(
                emails,
                "messaging/email/board_message",
                {"thread": thread, "message": message, "url": absolute_url(thread.get_absolute_url())},
            )
        )
    return message


@transaction.atomic
def start_thread(user, subject, body, notify=False):
    thread = BoardThread.objects.create(subject=subject, created_by=user)
    post_message(thread, user, body, notify=notify)
    return thread
