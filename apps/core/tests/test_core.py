import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.urls import reverse

from apps.accounts.models import BoardPosition
from apps.community.models import Home
from apps.core import audit, ratelimit
from apps.core.management.commands.daily_tasks import run_daily_tasks
from apps.core.models import AuditLog, HOASettings, TaskRun

pytestmark = pytest.mark.django_db


class TestSetupWizard:
    @pytest.fixture
    def fresh(self, hoa):
        hoa.setup_complete = False
        hoa.name = ""
        hoa.save()
        return hoa

    def test_everything_redirects_to_setup(self, client, fresh):
        response = client.get("/homes/")
        assert response.status_code == 302 and response.url == reverse("core:setup")

    def test_setup_creates_admin_and_completes(self, client, fresh):
        response = client.post(
            reverse("core:setup"),
            {
                "hoa_name": "Maple Glen HOA",
                "full_name": "Pat President",
                "email": "Pat@Example.com",
                "title": "president",
                "password1": "a-long-unique-passphrase",
                "password2": "a-long-unique-passphrase",
            },
        )
        assert response.status_code == 302
        user = get_user_model().objects.get(email="pat@example.com")
        assert user.is_superuser and user.is_board
        assert BoardPosition.objects.filter(user=user, title="president").exists()
        assert HOASettings.load().name == "Maple Glen HOA"
        assert HOASettings.load().setup_complete
        assert client.get(reverse("core:setup")).status_code == 404

    def test_setup_token_enforced(self, client, fresh, settings):
        settings.SETUP_TOKEN = "s3cret"
        data = {
            "hoa_name": "X",
            "full_name": "Y",
            "email": "y@example.com",
            "title": "president",
            "password1": "a-long-unique-passphrase",
            "password2": "a-long-unique-passphrase",
            "setup_token": "wrong",
        }
        assert client.post(reverse("core:setup"), data).status_code == 200
        assert not get_user_model().objects.exists()
        data["setup_token"] = "s3cret"
        assert client.post(reverse("core:setup"), data).status_code == 302

    def test_existing_admin_must_sign_in(self, client, fresh, admin_user, login):
        assert b"Sign in" in client.get(reverse("core:setup")).content
        login(admin_user)
        response = client.post(reverse("core:setup"), {"hoa_name": "Cedar Run"})
        assert response.status_code == 302
        assert HOASettings.load().setup_complete


class TestDailyTasks:
    def test_runs_and_records(self):
        run = run_daily_tasks()
        assert run.ok, run.summary
        assert "accounts" in run.summary and "community" in run.summary
        assert TaskRun.objects.count() == 1

    def test_command_is_repeatable(self):
        call_command("daily_tasks")
        call_command("daily_tasks")
        assert TaskRun.objects.filter(ok=True).count() == 2

    def test_skips_when_already_running(self):
        TaskRun.objects.create()
        assert run_daily_tasks() is None


class TestRateLimit:
    def test_counts_per_key(self):
        assert all(ratelimit.hit("t", "a", limit=3, period=60) for _ in range(3))
        assert not ratelimit.hit("t", "a", limit=3, period=60)
        assert ratelimit.hit("t", "b", limit=3, period=60)


class TestMisc:
    def test_audit_record(self, admin_user, home):
        audit.record(admin_user, "home.updated", home, {"lot_number": "9"})
        entry = AuditLog.objects.get()
        assert entry.actor == admin_user and entry.object_repr == "12 Elm Street"

    def test_healthz(self, client):
        assert client.get("/healthz").content == b"ok"

    def test_dashboard_for_board(self, login, board_user, home):
        response = login(board_user).get(reverse("core:dashboard"))
        assert response.status_code == 200
        assert response.context["board_stats"]["homes"] == 1

    def test_seed_demo_refuses_without_debug(self):
        with pytest.raises(CommandError):
            call_command("seed_demo")

    def test_seed_demo(self):
        call_command("seed_demo", "--allow-production", "--homes", "10")
        assert Home.objects.count() == 10
        with pytest.raises(CommandError):
            call_command("seed_demo", "--allow-production")
