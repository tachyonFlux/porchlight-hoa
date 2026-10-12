from datetime import timedelta

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from icalendar import Calendar

from apps.meetings import services
from apps.meetings.models import Audience, CalendarFeedToken, Meeting, Poll, PollOption, PollVote

pytestmark = pytest.mark.django_db


def at(days, hour=19):
    base = timezone.localtime().replace(hour=hour, minute=0, second=0, microsecond=0)
    return base + timedelta(days=days)


@pytest.fixture
def poll(board_user):
    poll = Poll.objects.create(title="Spring meeting", created_by=board_user, location="Clubhouse")
    for d in (7, 8, 9):
        PollOption.objects.create(poll=poll, starts_at=at(d), ends_at=at(d) + timedelta(hours=1))
    return poll


def vote(poll, user, choices):
    options = list(poll.options.all())
    services.record_votes(poll, user, {options[i].pk: c for i, c in enumerate(choices)})


class TestTally:
    def test_winner_prefers_yes_then_maybe_then_earliest(self, poll, resident, neighbor, board_user):
        vote(poll, resident, ["maybe", "yes", "yes"])
        vote(poll, neighbor, ["yes", "maybe", "yes"])
        vote(poll, board_user, ["no", "yes", "maybe"])
        # option 2: yes,maybe,yes -> 2+1+2=5 ; option 3: yes,yes,maybe -> 5 with 2 yes each -> earliest wins
        assert services.suggested_option(poll) == list(poll.options.all())[1]

    def test_no_votes_no_suggestion(self, poll):
        assert services.suggested_option(poll) is None

    def test_changing_vote_replaces_it(self, poll, resident):
        vote(poll, resident, ["yes", "no", "no"])
        vote(poll, resident, ["no", "no", "yes"])
        assert PollVote.objects.filter(user=resident).count() == 3
        assert services.voter_count(poll) == 1
        assert [t.yes for t in services.tally(poll)] == [0, 0, 1]

    def test_closed_or_expired_poll_rejects_votes(self, poll, resident):
        poll.closes_at = timezone.now() - timedelta(minutes=1)
        poll.save()
        with pytest.raises(ValueError):
            vote(poll, resident, ["yes", "yes", "yes"])

    def test_foreign_option_ignored(self, poll, resident, board_user):
        other = Poll.objects.create(title="Other", created_by=board_user)
        foreign = PollOption.objects.create(poll=other, starts_at=at(1), ends_at=at(1))
        services.record_votes(poll, resident, {foreign.pk: "yes"})
        assert not PollVote.objects.exists()


class TestPollToMeeting:
    def test_close_creates_meeting(self, poll, board_user):
        option = list(poll.options.all())[2]
        meeting, created = services.close_poll(
            poll, option, {"kind": "general", "agenda": "1. Budget"}, actor=board_user
        )
        assert created
        poll.refresh_from_db()
        assert poll.status == "closed" and poll.chosen_option == option
        assert (meeting.starts_at, meeting.ends_at) == (option.starts_at, option.ends_at)
        assert meeting.title == "Spring meeting" and meeting.location == "Clubhouse"
        assert meeting.poll == poll and meeting.audience == poll.audience

    def test_close_is_idempotent(self, poll, board_user):
        option = poll.options.first()
        first, created = services.close_poll(poll, option, {}, actor=board_user)
        second, created_again = services.close_poll(poll, option, {}, actor=board_user)
        assert first.pk == second.pk and Meeting.objects.count() == 1
        assert created and not created_again

    def test_option_from_other_poll_rejected(self, poll, board_user):
        other = Poll.objects.create(title="Other", created_by=board_user)
        foreign = PollOption.objects.create(poll=other, starts_at=at(1), ends_at=at(1))
        with pytest.raises(ValueError):
            services.close_poll(poll, foreign, {}, actor=board_user)

    def test_close_view_defaults_to_suggestion_and_emails(self, login, poll, board_user, resident, neighbor):
        vote(poll, resident, ["no", "yes", "no"])
        client = login(board_user)
        response = client.get(reverse("meetings:poll_close", args=[poll.pk]))
        winner = list(poll.options.all())[1]
        assert response.context["form"].fields["option"].initial == winner.pk
        response = client.post(
            reverse("meetings:poll_close", args=[poll.pk]),
            {"option": winner.pk, "kind": "general", "virtual_link": "", "agenda": "", "notify": "on"},
        )
        meeting = Meeting.objects.get()
        assert response.url == meeting.get_absolute_url()
        recipients = sorted(m.to[0] for m in mail.outbox)
        assert recipients == sorted([board_user.email, resident.email, neighbor.email])


class TestPollCreateView:
    def test_create_with_options(self, login, board_user, resident):
        day = (timezone.localdate() + timedelta(days=10)).isoformat()
        data = {
            "title": "Pool committee",
            "description": "",
            "audience": "residents",
            "location": "",
            "closes_at": "",
            "notify": "on",
            "date_0": day,
            "start_0": "19:00",
            "end_0": "20:00",
            "date_1": day,
            "start_1": "18:00",
            "end_1": "19:00",
            "date_2": day,
            "start_2": "",
            "end_2": "",
        }
        response = login(board_user).post(reverse("meetings:poll_create"), data)
        assert response.status_code == 200  # half-filled row 2 is an error
        data["date_2"] = ""
        response = login(board_user).post(reverse("meetings:poll_create"), data)
        poll = Poll.objects.get()
        starts = [timezone.localtime(o.starts_at).hour for o in poll.options.all()]
        assert starts == [18, 19]
        assert resident.email in [m.to[0] for m in mail.outbox]

    def test_overnight_option(self, login, board_user):
        day = (timezone.localdate() + timedelta(days=3)).isoformat()
        login(board_user).post(
            reverse("meetings:poll_create"),
            {"title": "Late", "audience": "board", "date_0": day, "start_0": "23:00", "end_0": "00:30"},
        )
        option = PollOption.objects.get()
        assert option.ends_at - option.starts_at == timedelta(minutes=90)

    def test_resident_cannot_create(self, login, resident):
        assert login(resident).get(reverse("meetings:poll_create")).status_code == 403


class TestVisibility:
    @pytest.fixture
    def board_meeting(self, board_user):
        return Meeting.objects.create(
            title="Exec session", audience=Audience.BOARD, starts_at=at(3), ends_at=at(3) + timedelta(hours=1)
        )

    @pytest.fixture
    def public_meeting(self):
        return Meeting.objects.create(title="Annual", starts_at=at(5), ends_at=at(5) + timedelta(hours=2))

    def test_resident_sees_only_resident_meetings(self, login, resident, board_meeting, public_meeting):
        client = login(resident)
        response = client.get(reverse("meetings:list"))
        assert list(response.context["upcoming"]) == [public_meeting]
        assert client.get(board_meeting.get_absolute_url()).status_code == 403
        assert client.get(reverse("meetings:meeting_ics", args=[board_meeting.pk])).status_code == 403

    def test_board_poll_hidden_from_residents(self, login, resident, board_user):
        poll = Poll.objects.create(title="Board only", audience=Audience.BOARD, created_by=board_user)
        assert login(resident).get(poll.get_absolute_url()).status_code == 403

    def test_unlinked_user_sees_nothing(self, login, make_user, public_meeting):
        client = login(make_user("nobody@example.com"))
        assert client.get(reverse("meetings:list")).status_code == 403
        assert client.get(public_meeting.get_absolute_url()).status_code == 403

    def test_vote_view(self, login, resident, poll):
        options = list(poll.options.all())
        response = login(resident).post(
            poll.get_absolute_url(), {f"option_{options[0].pk}": "yes", f"option_{options[1].pk}": "maybe"}
        )
        assert response.status_code == 302
        assert dict(PollVote.objects.values_list("option_id", "choice")) == {
            options[0].pk: "yes",
            options[1].pk: "maybe",
        }

    def test_vote_names_visible_only_to_board(self, login, resident, neighbor, board_user, poll):
        vote(poll, neighbor, ["yes", "no", "no"])
        assert login(resident).get(poll.get_absolute_url()).context["voters"] is None
        assert login(board_user).get(poll.get_absolute_url()).context["voters"]


class TestCalendar:
    @pytest.fixture
    def meetings(self):
        return [
            Meeting.objects.create(
                title="Annual",
                starts_at=at(5),
                ends_at=at(5) + timedelta(hours=2),
                location="Clubhouse",
                agenda="Budget",
            ),
            Meeting.objects.create(
                title="Exec", audience=Audience.BOARD, starts_at=at(6), ends_at=at(6) + timedelta(hours=1)
            ),
        ]

    def test_single_ics(self, login, resident, meetings):
        response = login(resident).get(reverse("meetings:meeting_ics", args=[meetings[0].pk]))
        cal = Calendar.from_ical(response.content)
        event = cal.walk("VEVENT")[0]
        assert str(event["summary"]) == "Annual" and str(event["location"]) == "Clubhouse"
        assert event["dtstart"].dt == meetings[0].starts_at
        assert str(event["uid"]) == f"{meetings[0].uid}@porchlight"

    def test_feed_respects_visibility_and_needs_no_login(self, client, resident, board_user, meetings):
        resident_feed = CalendarFeedToken.for_user(resident)
        board_feed = CalendarFeedToken.for_user(board_user)
        get = lambda feed: [  # noqa: E731
            str(e["summary"])
            for e in Calendar.from_ical(
                client.get(reverse("meetings:calendar_feed", args=[feed.token])).content
            ).walk("VEVENT")
        ]
        assert get(resident_feed) == ["Annual"]
        assert get(board_feed) == ["Annual"]  # board-only meetings are opt-in
        board_feed.include_board = True
        board_feed.save()
        assert sorted(get(board_feed)) == ["Annual", "Exec"]

    def test_feed_token_regeneration_and_bad_tokens(self, login, client, resident, meetings):
        old = CalendarFeedToken.for_user(resident).token
        login(resident).post(reverse("meetings:calendar"))
        assert CalendarFeedToken.objects.get(user=resident).token != old
        client.logout()
        assert client.get(reverse("meetings:calendar_feed", args=[old])).status_code == 404

    def test_feed_stops_for_former_resident(self, client, resident, meetings):
        from apps.community.models import Residency

        token = CalendarFeedToken.for_user(resident).token
        Residency.objects.filter(user=resident).update(ended_on=timezone.localdate())
        content = client.get(reverse("meetings:calendar_feed", args=[token])).content
        assert list(Calendar.from_ical(content).walk("VEVENT")) == []

    def test_cancel_marks_event_cancelled_and_bumps_sequence(self, login, board_user, meetings):
        login(board_user).post(reverse("meetings:meeting_cancel", args=[meetings[0].pk]))
        meetings[0].refresh_from_db()
        assert meetings[0].cancelled_at and meetings[0].sequence == 1
        event = Calendar.from_ical(services.calendar_for([meetings[0]])).walk("VEVENT")[0]
        assert str(event["status"]) == "CANCELLED" and event["sequence"] == 1


class TestMinutes:
    def test_upload_and_private_download(self, login, board_user, resident, make_user):
        meeting = Meeting.objects.create(
            title="Annual", starts_at=at(-5), ends_at=at(-5) + timedelta(hours=1)
        )
        client = login(board_user)
        pdf = SimpleUploadedFile("minutes.pdf", b"%PDF-1.4 minutes", content_type="application/pdf")
        data = {
            "title": "Annual",
            "kind": "annual",
            "audience": "residents",
            "starts_at": timezone.localtime(meeting.starts_at).strftime("%Y-%m-%dT%H:%M"),
            "ends_at": timezone.localtime(meeting.ends_at).strftime("%Y-%m-%dT%H:%M"),
            "location": "",
            "virtual_link": "",
            "agenda": "",
            "minutes_text": "Approved budget.",
            "minutes_file": pdf,
        }
        assert client.post(reverse("meetings:meeting_edit", args=[meeting.pk]), data).status_code == 302
        meeting.refresh_from_db()
        assert meeting.minutes_file.name.startswith("private/minutes/") and meeting.sequence == 1
        response = login(resident).get(reverse("meetings:minutes_file", args=[meeting.pk]))
        assert response.status_code == 200 and response["Content-Type"] == "application/pdf"
        assert b"".join(response.streaming_content) == b"%PDF-1.4 minutes"
        outsider = make_user("out@example.com")
        assert login(outsider).get(reverse("meetings:minutes_file", args=[meeting.pk])).status_code == 403

    def test_spoofed_file_rejected(self, login, board_user):
        fake = SimpleUploadedFile(
            "minutes.pdf", b"<html><script>alert(1)</script>", content_type="application/pdf"
        )
        start = timezone.localtime(at(2)).strftime("%Y-%m-%dT%H:%M")
        end = timezone.localtime(at(2) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        response = login(board_user).post(
            reverse("meetings:meeting_create"),
            {
                "title": "X",
                "kind": "general",
                "audience": "residents",
                "starts_at": start,
                "ends_at": end,
                "minutes_file": fake,
            },
        )
        assert response.status_code == 200 and "minutes_file" in response.context["form"].errors
        assert not Meeting.objects.exists()

    def test_end_before_start_rejected(self, login, board_user):
        start = timezone.localtime(at(2)).strftime("%Y-%m-%dT%H:%M")
        response = login(board_user).post(
            reverse("meetings:meeting_create"),
            {"title": "X", "kind": "general", "audience": "residents", "starts_at": start, "ends_at": start},
        )
        assert response.status_code == 200 and not Meeting.objects.exists()


def test_dashboard_shows_next_meeting_and_open_poll(login, resident, poll):
    Meeting.objects.create(title="Picnic", starts_at=at(2), ends_at=at(2) + timedelta(hours=2))
    response = login(resident).get("/")
    assert response.context["next_meeting"].title == "Picnic"
    assert response.context["polls_to_answer"] == [poll]
