import pytest
from django.core import mail
from django.urls import reverse

from apps.messaging import services
from apps.messaging.models import Announcement, BoardThread, CourtesyNotice

pytestmark = pytest.mark.django_db


class TestAnnouncements:
    def test_post_and_email(self, login, board_user, resident, neighbor):
        response = login(board_user).post(
            reverse("messaging:announcement_create"),
            {
                "title": "Pool opens Saturday",
                "body": "See you there!",
                "audience": "residents",
                "send_email": "on",
            },
        )
        item = Announcement.objects.get()
        assert response.url == item.get_absolute_url()
        assert sorted(m.to[0] for m in mail.outbox) == sorted(
            [board_user.email, resident.email, neighbor.email]
        )
        item.refresh_from_db()
        assert item.email_count == 3 and item.emailed_at

    def test_board_only_hidden(self, login, board_user, resident):
        item = Announcement.objects.create(
            title="Budget draft", body="x", audience="board", created_by=board_user
        )
        client = login(resident)
        assert client.get(item.get_absolute_url()).status_code == 404
        assert list(client.get(reverse("messaging:announcements")).context["items"]) == []

    def test_residents_cannot_post(self, login, resident):
        assert login(resident).get(reverse("messaging:announcement_create")).status_code == 403

    def test_unlinked_user_blocked(self, login, make_user):
        assert login(make_user("x@example.com")).get(reverse("messaging:announcements")).status_code == 403

    def test_daily_email_cap(self, login, board_user, resident):
        client = login(board_user)
        for i in range(11):
            client.post(
                reverse("messaging:announcement_create"),
                {"title": f"N{i}", "body": "b", "audience": "residents", "send_email": "on"},
            )
        assert Announcement.objects.count() == 11
        assert Announcement.objects.filter(emailed_at__isnull=True).count() == 1

    def test_pinned_first_on_dashboard(self, login, board_user, resident):
        Announcement.objects.create(title="Old pinned", body="x", pinned=True)
        Announcement.objects.create(title="Newer", body="x")
        titles = [a.title for a in login(resident).get("/").context["announcements"]]
        assert titles == ["Old pinned", "Newer"]


class TestCourtesyNotices:
    def test_create_from_template_and_email_household(self, login, board_user, resident, home):
        client = login(board_user)
        form = client.get(reverse("messaging:notice_create"), {"template": "trash", "home": home.pk}).context[
            "form"
        ]
        assert "trash" in form.initial["subject"].lower() and form.initial["home"] == home.pk
        client.post(
            reverse("messaging:notice_create"),
            {
                "home": home.pk,
                "subject": form.initial["subject"],
                "body": form.initial["body"],
                "send_email": "on",
            },
        )
        notice = CourtesyNotice.objects.get()
        assert notice.emailed_at and mail.outbox[-1].to == [resident.email]

    def test_household_sees_and_replies_neighbor_cannot(
        self, login, board_user, resident, neighbor, home, django_capture_on_commit_callbacks
    ):
        notice = CourtesyNotice.objects.create(
            home=home, subject="Cans", body="Please", created_by=board_user
        )
        assert login(neighbor).get(notice.get_absolute_url()).status_code == 403
        assert (
            login(neighbor)
            .post(reverse("messaging:notice_reply", args=[notice.pk]), {"body": "x"})
            .status_code
            == 403
        )
        client = login(resident)
        assert client.get(notice.get_absolute_url()).status_code == 200
        with django_capture_on_commit_callbacks(execute=True):
            client.post(reverse("messaging:notice_reply", args=[notice.pk]), {"body": "Done, sorry!"})
        assert notice.replies.count() == 1 and mail.outbox[-1].to == [board_user.email]
        assert login(resident).get("/").context["open_notices"] == [notice]

    def test_resolve_and_reopen(self, login, board_user, resident, home):
        notice = CourtesyNotice.objects.create(home=home, subject="Cans", body="Please")
        client = login(board_user)
        client.post(reverse("messaging:notice_resolve", args=[notice.pk]), {"note": "Thanks!"})
        notice.refresh_from_db()
        assert notice.status == "resolved" and notice.resolved_by == board_user
        client.post(reverse("messaging:notice_resolve", args=[notice.pk]))
        notice.refresh_from_db()
        assert notice.status == "open" and notice.resolved_at is None
        assert login(resident).post(reverse("messaging:notice_resolve", args=[notice.pk])).status_code == 403

    def test_list_scopes(self, login, board_user, resident, neighbor, home):
        CourtesyNotice.objects.create(home=home, subject="Cans", body="x")
        assert len(login(resident).get(reverse("messaging:notices")).context["notices"]) == 1
        assert len(login(neighbor).get(reverse("messaging:notices")).context["notices"]) == 0
        assert len(login(board_user).get(reverse("messaging:notices")).context["notices"]) == 1


class TestBoardRoom:
    def test_board_only(self, login, resident):
        client = login(resident)
        assert client.get(reverse("messaging:board_room")).status_code == 403
        thread = BoardThread.objects.create(subject="Secret")
        assert client.get(thread.get_absolute_url()).status_code == 403

    def test_thread_flow_and_unread(self, login, board_user, admin_user, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            login(board_user).post(
                reverse("messaging:thread_create"),
                {"subject": "Landscaping bids", "body": "Three came in.", "notify": "on"},
            )
        thread = BoardThread.objects.get()
        assert mail.outbox[-1].to == [admin_user.email]
        assert services.unread_count(admin_user) == 1 and services.unread_count(board_user) == 0
        client = login(admin_user)
        assert client.get("/").context["board_unread"] == 1
        client.get(thread.get_absolute_url())
        assert services.unread_count(admin_user) == 0
        client.post(thread.get_absolute_url(), {"body": "Let's go with #2"})
        assert thread.messages.count() == 2
        assert services.unread_count(board_user) == 1
