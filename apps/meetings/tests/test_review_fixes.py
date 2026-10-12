"""Regression tests for issues found in the slice-3 cross review."""

import io
import zipfile
from datetime import date, timedelta
from unittest import mock

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from icalendar import Calendar

from apps.core import email as core_email
from apps.core.uploads import validate_upload
from apps.meetings import services
from apps.meetings.forms import PollForm
from apps.meetings.models import CalendarFeedToken, Meeting, Poll, PollOption

pytestmark = pytest.mark.django_db


def at(days, hour=19):
    base = timezone.localtime().replace(hour=hour, minute=0, second=0, microsecond=0)
    return base + timedelta(days=days)


def _form_data(meeting, **extra):
    fmt = "%Y-%m-%dT%H:%M"
    data = {
        "title": meeting.title,
        "kind": meeting.kind,
        "audience": meeting.audience,
        "starts_at": timezone.localtime(meeting.starts_at).strftime(fmt),
        "ends_at": timezone.localtime(meeting.ends_at).strftime(fmt),
        "location": "",
        "virtual_link": "",
        "agenda": "",
        "minutes_text": "",
    }
    data.update(extra)
    return data


def test_replaced_and_cleared_minutes_files_are_deleted(
    login, board_user, django_capture_on_commit_callbacks
):
    meeting = Meeting.objects.create(title="Annual", starts_at=at(-3), ends_at=at(-3) + timedelta(hours=1))
    client = login(board_user)
    url = reverse("meetings:meeting_edit", args=[meeting.pk])

    def upload(name):
        f = SimpleUploadedFile(name, b"%PDF-1.4 x", content_type="application/pdf")
        with django_capture_on_commit_callbacks(execute=True):
            client.post(url, _form_data(meeting, minutes_file=f))
        meeting.refresh_from_db()
        return meeting.minutes_file.name

    first = upload("a.pdf")
    storage = meeting.minutes_file.storage
    second = upload("b.pdf")
    assert first != second and not storage.exists(first) and storage.exists(second)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(url, _form_data(meeting, **{"minutes_file-clear": "on"}))
    meeting.refresh_from_db()
    assert not meeting.minutes_file and not storage.exists(second)


def test_bulk_send_continues_past_a_refused_address(settings):
    sent = []

    class Flaky:
        def open(self):
            pass

        def close(self):
            pass

        def send_messages(self, messages):
            if messages[0].to == ["bad@example.com"]:
                raise ConnectionError("refused")
            sent.extend(m.to[0] for m in messages)

    with mock.patch.object(core_email, "get_connection", lambda: Flaky()):
        core_email.send_bulk(
            ["a@example.com", "bad@example.com", "c@example.com"],
            "meetings/email/meeting_cancelled",
            {"meeting": Meeting(title="X", starts_at=at(1), ends_at=at(1)), "url": "u"},
        )
    assert sent == ["a@example.com", "c@example.com"]


def test_plain_text_email_not_html_escaped(resident, board_user):
    meeting = Meeting.objects.create(title="Tom & Jerry's <party>", starts_at=at(2), ends_at=at(2))
    core_email.send_bulk(
        [resident.email], "meetings/email/meeting_scheduled", {"meeting": meeting, "url": "u"}
    )
    assert "Tom & Jerry's <party>" in mail.outbox[0].subject
    assert "&amp;" not in mail.outbox[0].body


def test_double_close_emails_once(login, board_user, resident):
    poll = Poll.objects.create(title="P", created_by=board_user)
    option = PollOption.objects.create(poll=poll, starts_at=at(5), ends_at=at(5) + timedelta(hours=1))
    services.close_poll(poll, option, {}, actor=board_user)  # first click already processed
    poll.status = Poll.Status.OPEN  # second request read the poll before the first committed
    poll.save(update_fields=["status"])
    login(board_user).post(
        reverse("meetings:poll_close", args=[poll.pk]),
        {"option": option.pk, "kind": "general", "notify": "on"},
    )
    assert mail.outbox == [] and Meeting.objects.count() == 1


def test_mass_email_capped_and_audited(login, board_user, resident):
    from apps.core.models import AuditLog

    client = login(board_user)
    meeting = Meeting.objects.create(title="M", starts_at=at(3), ends_at=at(3) + timedelta(hours=1))
    for _ in range(12):
        client.post(reverse("meetings:meeting_edit", args=[meeting.pk]), _form_data(meeting, notify="on"))
    assert len(mail.outbox) == 20  # 10 sends x 2 recipients
    assert AuditLog.objects.filter(action="meetings.emailed").count() == 10


class TestFeed:
    def test_not_rate_limited_for_valid_token(self, client, resident):
        token = CalendarFeedToken.for_user(resident).token
        url = reverse("meetings:calendar_feed", args=[token])
        assert all(client.get(url).status_code == 200 for _ in range(150))

    def test_unknown_tokens_rate_limited(self, client):
        codes = [
            client.get(reverse("meetings:calendar_feed", args=[f"nope{i}"])).status_code for i in range(31)
        ]
        assert codes[-1] == 429

    def test_link_rotates_when_board_role_changes(self, login, admin_user, resident):
        old = CalendarFeedToken.for_user(resident).token
        login(admin_user).post(
            reverse("accounts:board_add"),
            {
                "email": resident.email,
                "title": "secretary",
                "custom_title": "",
                "started_on": timezone.localdate().isoformat(),
            },
        )
        assert not CalendarFeedToken.objects.filter(token=old).exists()

    def test_board_opt_in_needs_board(self, login, resident):
        login(resident).post(reverse("meetings:calendar"), {"include_board": "1"})
        assert not CalendarFeedToken.objects.get(user=resident).include_board

    def test_single_ics_has_charset(self, login, resident):
        meeting = Meeting.objects.create(title="Café", starts_at=at(2), ends_at=at(2) + timedelta(hours=1))
        response = login(resident).get(reverse("meetings:meeting_ics", args=[meeting.pk]))
        assert "charset=utf-8" in response["Content-Type"]
        assert str(Calendar.from_ical(response.content).walk("VEVENT")[0]["summary"]) == "Café"

    def test_cancel_updates_dtstamp(self, login, board_user):
        meeting = Meeting.objects.create(title="M", starts_at=at(2), ends_at=at(2) + timedelta(hours=1))
        Meeting.objects.filter(pk=meeting.pk).update(updated_at=timezone.now() - timedelta(days=3))
        login(board_user).post(reverse("meetings:meeting_cancel", args=[meeting.pk]))
        meeting.refresh_from_db()
        assert timezone.now() - meeting.updated_at < timedelta(minutes=1)


class TestPollForm:
    def _form(self, **row):
        data = {"title": "T", "audience": "residents"}
        data.update(row)
        return PollForm(data)

    def test_dst_gap_rejected(self, settings):
        settings.TIME_ZONE = "America/New_York"
        timezone.activate("America/New_York")
        form = self._form(date_0="2027-03-14", start_0="02:30", end_0="03:30")
        assert not form.is_valid()
        timezone.deactivate()

    def test_equal_start_and_end_rejected(self):
        form = self._form(
            date_0=(date.today() + timedelta(days=3)).isoformat(), start_0="13:00", end_0="13:00"
        )
        assert not form.is_valid() and "end_0" in form.errors


def test_expired_poll_not_listed_as_votable(login, board_user, resident):
    Poll.objects.create(title="Old", created_by=board_user, closes_at=timezone.now() - timedelta(hours=1))
    assert login(resident).get(reverse("meetings:list")).context["open_polls"] == []


def test_empty_vote_submission_says_so(login, board_user, resident):
    poll = Poll.objects.create(title="P", created_by=board_user)
    PollOption.objects.create(poll=poll, starts_at=at(5), ends_at=at(5) + timedelta(hours=1))
    response = login(resident).post(poll.get_absolute_url(), {}, follow=True)
    assert "at least one" in response.content.decode()


class TestUploadValidation:
    def _f(self, name, content):
        return SimpleUploadedFile(name, content)

    def test_webp_must_say_webp(self):
        with pytest.raises(ValidationError):
            validate_upload(self._f("x.webp", b"RIFF\x00\x00\x00\x00WAVEfmt "), allowed=("webp",))
        validate_upload(self._f("x.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 "), allowed=("webp",))

    def test_heic_brand_checked(self):
        with pytest.raises(ValidationError):
            validate_upload(self._f("x.heic", b"\x00\x00\x00\x18ftypisom\x00\x00"), allowed=("heic",))
        validate_upload(self._f("x.heic", b"\x00\x00\x00\x18ftypheic\x00\x00"), allowed=("heic",))

    def test_docx_needs_office_zip(self):
        plain = io.BytesIO()
        with zipfile.ZipFile(plain, "w") as z:
            z.writestr("evil.html", "<script>")
        with pytest.raises(ValidationError):
            validate_upload(self._f("x.docx", plain.getvalue()), allowed=("docx",))
        office = io.BytesIO()
        with zipfile.ZipFile(office, "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
        validate_upload(self._f("x.docx", office.getvalue()), allowed=("docx",))


def test_oversized_request_rejected_before_view(client, settings):
    settings.MAX_REQUEST_MB = 1
    big = SimpleUploadedFile("big.pdf", b"%PDF-" + b"0" * (2 * 1024 * 1024))
    response = client.post(reverse("accounts:login"), {"email": "a@example.com", "f": big})
    assert response.status_code == 400
