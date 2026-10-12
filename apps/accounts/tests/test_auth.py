import re
from datetime import timedelta

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import BoardPosition, MagicLinkToken

pytestmark = pytest.mark.django_db


def _link_from_mail():
    body = mail.outbox[-1].body
    return re.search(r"http://testserver(/login/link/[^\s]+)", body).group(1)


def _totp_token(device):
    return TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift).token()


def _confirmed_device(user):
    return TOTPDevice.objects.create(user=user, name="phone", confirmed=True)


class TestMagicLink:
    def test_known_email_gets_link_and_unknown_gets_same_response(self, client, resident):
        r1 = client.post(reverse("accounts:login"), {"email": "RESIDENT@example.com "})
        r2 = client.post(reverse("accounts:login"), {"email": "stranger@example.com"})
        assert r1.status_code == r2.status_code == 302
        assert r1.url == r2.url == reverse("accounts:login_sent")
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["resident@example.com"]

    def test_get_does_not_consume_and_post_logs_in(self, client, resident):
        client.post(reverse("accounts:login"), {"email": resident.email})
        path = _link_from_mail()
        assert client.get(path).status_code == 200  # e.g. an email scanner
        response = client.post(path)
        assert response.status_code == 302
        assert client.session["_auth_user_id"] == str(resident.pk)

    def test_link_is_single_use(self, client, resident):
        client.post(reverse("accounts:login"), {"email": resident.email})
        path = _link_from_mail()
        client.post(path)
        client.logout()
        assert client.post(path).status_code == 410
        assert "_auth_user_id" not in client.session

    def test_expired_link_rejected(self, client, resident):
        _, raw = MagicLinkToken.issue(resident)
        MagicLinkToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        assert client.post(reverse("accounts:magic_link", args=[raw])).status_code == 410

    def test_inactive_user_link_rejected(self, client, resident):
        _, raw = MagicLinkToken.issue(resident)
        resident.is_active = False
        resident.save()
        assert client.post(reverse("accounts:magic_link", args=[raw])).status_code == 410

    def test_only_hash_is_stored(self, resident):
        token, raw = MagicLinkToken.issue(resident)
        assert raw not in token.token_hash
        assert len(token.token_hash) == 64

    def test_next_must_be_local(self, client, resident):
        _, raw = MagicLinkToken.issue(resident)
        url = reverse("accounts:magic_link", args=[raw]) + "?next=https://evil.example/"
        response = client.post(url)
        assert response.url == reverse("core:dashboard")

    def test_per_email_rate_limit(self, client, resident):
        for _ in range(5):
            client.post(reverse("accounts:login"), {"email": resident.email})
        response = client.post(reverse("accounts:login"), {"email": resident.email})
        assert response.status_code == 429
        assert len(mail.outbox) == 5


class TestPasswordAndTotp:
    def test_board_password_login(self, client, board_user):
        response = client.post(
            reverse("accounts:password_login"),
            {"email": board_user.email, "password": "correct-horse-battery"},
        )
        assert response.status_code == 302
        assert client.session["_auth_user_id"] == str(board_user.pk)

    def test_resident_without_password_cannot_password_login(self, client, resident):
        response = client.post(reverse("accounts:password_login"), {"email": resident.email, "password": ""})
        assert response.status_code == 200
        assert "_auth_user_id" not in client.session

    def test_password_login_rate_limited(self, client, board_user):
        for _ in range(10):
            client.post(
                reverse("accounts:password_login"), {"email": board_user.email, "password": "wrong-password"}
            )
        response = client.post(
            reverse("accounts:password_login"),
            {"email": board_user.email, "password": "correct-horse-battery"},
        )
        assert response.status_code == 429

    def test_totp_required_after_password(self, client, board_user):
        device = _confirmed_device(board_user)
        response = client.post(
            reverse("accounts:password_login"),
            {"email": board_user.email, "password": "correct-horse-battery"},
        )
        assert response.url == reverse("accounts:otp_verify")
        assert "_auth_user_id" not in client.session

        bad = client.post(reverse("accounts:otp_verify"), {"token": "000000"})
        assert bad.status_code == 200 and "_auth_user_id" not in client.session
        device.refresh_from_db()
        device.throttle_reset()  # django-otp backs off briefly after a wrong code

        ok = client.post(reverse("accounts:otp_verify"), {"token": _totp_token(device)})
        assert ok.status_code == 302
        assert client.session["_auth_user_id"] == str(board_user.pk)
        assert client.get(reverse("community:board_homes")).status_code == 200

    def test_totp_also_required_after_magic_link(self, client, board_user):
        _confirmed_device(board_user)
        _, raw = MagicLinkToken.issue(board_user)
        response = client.post(reverse("accounts:magic_link", args=[raw]))
        assert response.url == reverse("accounts:otp_verify")
        assert "_auth_user_id" not in client.session

    def test_board_pages_require_verified_session(self, login, board_user):
        _confirmed_device(board_user)
        client = login(board_user)  # logged in but never passed the OTP step
        response = client.get(reverse("community:board_homes"))
        assert response.status_code == 302
        assert response.url == reverse("accounts:otp_verify")

    def test_totp_setup_flow(self, login, board_user):
        client = login(board_user)
        client.get(reverse("accounts:totp_setup"))
        device = TOTPDevice.objects.get(user=board_user, confirmed=False)
        response = client.post(reverse("accounts:totp_setup"), {"token": _totp_token(device)})
        assert response.status_code == 302
        device.refresh_from_db()
        assert device.confirmed and board_user.has_totp


class TestRoles:
    def test_resident_cannot_open_board_pages(self, login, resident):
        client = login(resident)
        assert client.get(reverse("community:board_homes")).status_code == 403
        assert client.get(reverse("accounts:board_add")).status_code == 403

    def test_board_member_is_not_admin(self, login, board_user):
        client = login(board_user)
        assert client.get(reverse("community:board_homes")).status_code == 200
        assert client.get(reverse("accounts:board_add")).status_code == 403
        assert client.get(reverse("core:settings")).status_code == 403

    def test_anonymous_redirected_to_login(self, client):
        response = client.get(reverse("community:board_homes"))
        assert response.status_code == 302
        assert response.url.startswith(reverse("accounts:login"))

    def test_admin_adds_and_ends_board_position(self, login, admin_user):
        client = login(admin_user)
        response = client.post(
            reverse("accounts:board_add"),
            {
                "email": "New.Sec@example.com",
                "full_name": "Sam Sec",
                "title": "secretary",
                "custom_title": "",
                "started_on": timezone.localdate().isoformat(),
            },
        )
        assert response.status_code == 302
        position = BoardPosition.objects.get(user__email="new.sec@example.com")
        assert position.user.is_board
        client.post(reverse("accounts:board_end", args=[position.pk]))
        position.refresh_from_db()
        assert position.ended_on == timezone.localdate()
        assert not position.user.is_board

    def test_future_and_past_positions_do_not_count(self, make_user):
        user = make_user("x@example.com")
        today = timezone.localdate()
        BoardPosition.objects.create(user=user, started_on=today + timedelta(days=3))
        BoardPosition.objects.create(
            user=user, started_on=today - timedelta(days=400), ended_on=today - timedelta(days=30)
        )
        assert not user.is_board
