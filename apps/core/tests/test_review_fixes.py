"""Regression tests for issues found in the slice-1 cross review."""

from datetime import timedelta

import pytest
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.core import setup_token
from apps.core.management.commands.scheduler import should_run
from apps.core.models import TaskRun
from apps.core.utils import client_ip

pytestmark = pytest.mark.django_db


class TestClientIp:
    def _req(self, xff):
        return RequestFactory().get("/", HTTP_X_FORWARDED_FOR=xff, REMOTE_ADDR="10.0.0.2")

    @override_settings(BEHIND_PROXY=True, TRUSTED_PROXY_COUNT=1)
    def test_spoofed_left_entries_ignored(self):
        assert client_ip(self._req("6.6.6.6, 203.0.113.9")) == "203.0.113.9"

    @override_settings(BEHIND_PROXY=True, TRUSTED_PROXY_COUNT=2)
    def test_two_proxies(self):
        assert client_ip(self._req("6.6.6.6, 203.0.113.9, 172.16.0.1")) == "203.0.113.9"

    @override_settings(BEHIND_PROXY=False)
    def test_header_ignored_without_proxy(self):
        assert client_ip(self._req("6.6.6.6")) == "10.0.0.2"


class TestSetupToken:
    @pytest.fixture
    def fresh(self, hoa, settings, tmp_path):
        settings.DATA_DIR = tmp_path
        settings.SETUP_TOKEN_REQUIRED = True
        hoa.setup_complete = False
        hoa.save()

    def _data(self, token):
        return {
            "hoa_name": "X",
            "full_name": "Y",
            "email": "y@example.com",
            "title": "president",
            "password1": "a-long-unique-passphrase",
            "password2": "a-long-unique-passphrase",
            "setup_token": token,
        }

    def test_generated_token_required_and_discarded(self, client, fresh, tmp_path):
        client.get(reverse("core:setup"))
        token = (tmp_path / "setup-token.txt").read_text()
        assert client.post(reverse("core:setup"), self._data("nope")).status_code == 200
        assert client.post(reverse("core:setup"), self._data("ünïcode")).status_code == 200
        assert client.post(reverse("core:setup"), self._data(token)).status_code == 302
        assert not (tmp_path / "setup-token.txt").exists()

    def test_matches_handles_non_ascii(self, fresh):
        assert setup_token.matches("ünïcode") is False


class TestTwoStepPolicy:
    def test_required_two_step_redirects_to_setup(self, login, board_user, hoa):
        hoa.require_board_two_step = True
        hoa.save()
        client = login(board_user)
        response = client.get(reverse("community:board_homes"))
        assert response.url == reverse("accounts:totp_setup")
        assert client.get(reverse("accounts:totp_setup")).status_code == 200  # no loop


def test_password_login_limited_per_account(client, board_user):
    for i in range(20):
        client.post(
            reverse("accounts:password_login"),
            {"email": board_user.email, "password": "wrong"},
            REMOTE_ADDR=f"10.1.{i}.1",
        )
    response = client.post(
        reverse("accounts:password_login"),
        {"email": board_user.email, "password": "correct-horse-battery"},
        REMOTE_ADDR="10.9.9.9",
    )
    assert response.status_code == 429


def test_new_magic_link_retires_old_one(resident):
    from apps.accounts.models import MagicLinkToken

    _, first = MagicLinkToken.issue(resident)
    _, second = MagicLinkToken.issue(resident)
    assert MagicLinkToken.find_valid(first) is None
    assert MagicLinkToken.find_valid(second) is not None


class TestDjangoAdmin:
    def test_admin_requires_otp(self, client, admin_user):
        TOTPDevice.objects.create(user=admin_user, name="phone", confirmed=True)
        client.force_login(admin_user)
        response = client.get("/django-admin/accounts/user/")
        assert response.status_code == 302  # not verified -> sent to admin login


class TestScheduler:
    def _now(self, hour):
        return timezone.localtime().replace(hour=hour, minute=30, second=0, microsecond=0)

    def test_runs_once_after_hour(self):
        now = self._now(7)
        assert should_run(now, 6)
        TaskRun.objects.create(started_at=now - timedelta(minutes=5), ok=True)
        assert not should_run(now, 6)

    def test_failed_run_retried_hourly_up_to_three_times(self):
        now = self._now(10)
        TaskRun.objects.create(started_at=now - timedelta(minutes=10), ok=False)
        assert not should_run(now, 6)
        assert should_run(now + timedelta(hours=1), 6)
        TaskRun.objects.create(started_at=now - timedelta(hours=2), ok=False)
        TaskRun.objects.create(started_at=now - timedelta(hours=3), ok=False)
        assert not should_run(now + timedelta(hours=5), 6)

    def test_before_hour_uses_previous_window(self):
        now = self._now(3)
        TaskRun.objects.create(started_at=now - timedelta(hours=10), ok=True)  # yesterday 17:30
        assert not should_run(now, 6)
