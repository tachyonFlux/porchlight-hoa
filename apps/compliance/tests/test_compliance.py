from datetime import date, timedelta

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.compliance import services
from apps.compliance.models import Deadline, DeadlineReminder, add_years, parse_reminder_days

pytestmark = pytest.mark.django_db
TODAY = date(2027, 3, 1)


def make(due, **kw):
    return Deadline.objects.create(title=kw.pop("title", "Annual report"), due_date=due, **kw)


class TestHelpers:
    def test_parse_reminder_days(self):
        assert parse_reminder_days("30, 7,1, 7") == [30, 7, 1]
        assert parse_reminder_days("") == []
        for bad in ("soon", "-3", "400"):
            with pytest.raises(ValidationError):
                parse_reminder_days(bad)

    def test_add_years_leap_day(self):
        assert add_years(date(2028, 2, 29), 1) == date(2029, 2, 28)
        assert add_years(date(2027, 4, 15), 1) == date(2028, 4, 15)


class TestCompletion:
    def test_yearly_creates_next_once(self, board_user):
        d = make(date(2027, 4, 15), remind_days="14")
        nxt = services.complete(d, board_user, "Filed")
        assert nxt.due_date == date(2028, 4, 15) and nxt.remind_days == "14"
        assert services.complete(d, board_user) == nxt  # idempotent
        assert Deadline.objects.count() == 2

    def test_one_off_does_not_repeat(self, board_user):
        d = make(date(2027, 4, 15), recurrence="none")
        assert services.complete(d, board_user) is None
        assert Deadline.objects.count() == 1

    def test_reopen_removes_untouched_next(self, board_user):
        d = make(date(2027, 4, 15))
        services.complete(d, board_user)
        services.reopen(d, board_user)
        d.refresh_from_db()
        assert not d.is_done and Deadline.objects.count() == 1


class TestReminders:
    def test_closest_threshold_only_and_once(self, board_user):
        d = make(TODAY + timedelta(days=5), remind_days="30, 7, 1")  # added late: 30 and 7 already passed
        assert services.send_reminders(TODAY) == {"reminders": 1, "overdue": 0}
        assert sorted(DeadlineReminder.objects.values_list("days_before", flat=True)) == [7, 30]
        assert len(mail.outbox) == 1
        assert services.send_reminders(TODAY) == {"reminders": 0, "overdue": 0}
        services.send_reminders(d.due_date - timedelta(days=1))
        assert len(mail.outbox) == 2

    def test_owner_gets_it_instead_of_board(self, board_user, admin_user):
        make(TODAY + timedelta(days=1), remind_days="1", owner=board_user)
        services.send_reminders(TODAY)
        assert [m.to for m in mail.outbox] == [[board_user.email]]

    def test_whole_board_when_no_owner(self, board_user, admin_user):
        make(TODAY + timedelta(days=1), remind_days="1")
        services.send_reminders(TODAY)
        assert sorted(m.to[0] for m in mail.outbox) == sorted([board_user.email, admin_user.email])

    def test_overdue_alert_once(self, board_user):
        make(TODAY - timedelta(days=2))
        services.send_reminders(TODAY)
        services.send_reminders(TODAY + timedelta(days=1))
        assert len(mail.outbox) == 1 and "Overdue" in mail.outbox[0].subject

    def test_completed_never_reminded(self, board_user):
        d = make(TODAY + timedelta(days=1), remind_days="1", recurrence="none")
        services.complete(d, board_user)
        services.send_reminders(TODAY)
        assert mail.outbox == []

    def test_editing_due_date_resets_reminders(self, login, board_user):
        d = make(TODAY + timedelta(days=1), remind_days="1")
        services.send_reminders(TODAY)
        new_due = timezone.localdate() + timedelta(days=60)
        login(board_user).post(
            reverse("compliance:edit", args=[d.pk]),
            {
                "title": d.title,
                "category": "filing",
                "due_date": new_due.isoformat(),
                "recurrence": "yearly",
                "owner": "",
                "remind_days": "1",
                "description": "",
            },
        )
        assert not DeadlineReminder.objects.exists()


class TestSuggestionsAndViews:
    def test_suggestions_idempotent_and_hedged(self, login, board_user):
        client = login(board_user)
        client.post(reverse("compliance:suggestions"))
        count = Deadline.objects.count()
        assert count == len(services.MARYLAND_SUGGESTIONS)
        client.post(reverse("compliance:suggestions"))
        assert Deadline.objects.count() == count
        for d in Deadline.objects.all():
            assert d.due_date >= timezone.localdate()
            text = d.description.lower()
            assert any(w in text for w in ("confirm", "check", "ask", "set this")), d.title

    def test_board_only(self, login, resident):
        assert login(resident).get(reverse("compliance:list")).status_code == 403

    def test_list_groups_by_urgency(self, login, board_user):
        today = timezone.localdate()
        make(today - timedelta(days=1), title="Late")
        make(today + timedelta(days=3), title="Soon")
        make(today + timedelta(days=90), title="Later")
        ctx = login(board_user).get(reverse("compliance:list")).context
        assert [d.title for d in ctx["overdue"]] == ["Late"]
        assert [d.title for d in ctx["soon"]] == ["Soon"]
        assert [d.title for d in ctx["later"]] == ["Later"]

    def test_complete_view(self, login, board_user):
        d = make(timezone.localdate() + timedelta(days=3))
        login(board_user).post(reverse("compliance:complete", args=[d.pk]), {"note": "Done online"})
        d.refresh_from_db()
        assert d.is_done and d.completion_note == "Done online"

    def test_daily_task(self, board_user):
        from apps.core.management.commands.daily_tasks import run_daily_tasks

        make(timezone.localdate() + timedelta(days=1), remind_days="1")
        run = run_daily_tasks()
        assert run.summary["compliance"] == {"reminders": 1, "overdue": 0}
