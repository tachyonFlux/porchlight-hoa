from django.db import transaction
from django.utils import timezone

from apps.accounts.models import board_members
from apps.community.services import household_emails, is_household_member
from apps.core import audit
from apps.core.email import send_bulk
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


@transaction.atomic
def reply_to_notice(notice, user, body):
    reply = NoticeReply.objects.create(notice=notice, author=user, body=body)
    if is_household_member(user, notice.home):
        emails = board_members().exclude(pk=user.pk).values_list("email", flat=True)
        transaction.on_commit(
            lambda: send_bulk(
                emails,
                "messaging/email/notice_reply",
                {"notice": notice, "reply": reply, "url": absolute_url(notice.get_absolute_url())},
            )
        )
    return reply


def resolve_notice(notice, user, note="", request=None):
    notice.status = CourtesyNotice.Status.RESOLVED
    notice.resolved_at = timezone.now()
    notice.resolved_by = user
    notice.resolution_note = note
    notice.save()
    audit.record(request or user, "messaging.notice_resolved", notice)


def reopen_notice(notice, user, request=None):
    notice.status = CourtesyNotice.Status.OPEN
    notice.resolved_at = None
    notice.resolved_by = None
    notice.save()
    audit.record(request or user, "messaging.notice_reopened", notice)


# ------------------------------------------------------------------ board room


def threads_with_unread(user):
    threads = list(BoardThread.objects.select_related("created_by"))
    reads = dict(BoardThreadRead.objects.filter(user=user).values_list("thread_id", "last_read_at"))
    for thread in threads:
        last = reads.get(thread.pk)
        thread.unread = last is None or thread.last_activity_at > last
    return threads


def mark_read(thread, user):
    BoardThreadRead.objects.update_or_create(
        thread=thread, user=user, defaults={"last_read_at": timezone.now()}
    )


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
    mark_read(thread, user)
    if notify:
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
