"""Regression tests for issues found in the slice-4 cross review."""

from io import BytesIO

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from apps.community.models import Residency
from apps.exterior import services
from apps.exterior.models import ExteriorRequest, RequestPhoto
from apps.messaging import services as messaging
from apps.messaging.models import BoardThread, CourtesyNotice

pytestmark = pytest.mark.django_db
S = ExteriorRequest.Status


def jpeg_with_gps():
    img = Image.new("RGB", (8, 6), "teal")
    exif = Image.Exif()
    exif[0x8825] = {2: (39.0, 17.0, 0.0)}  # GPSInfo: latitude
    exif[0x0110] = "SecretPhone 12"  # camera model
    out = BytesIO()
    img.save(out, "JPEG", exif=exif.tobytes())
    return out.getvalue()


def png():
    out = BytesIO()
    Image.new("RGB", (4, 4), "teal").save(out, "PNG")
    return SimpleUploadedFile("p.png", out.getvalue(), content_type="image/png")


@pytest.fixture
def req(resident, home):
    return services.submit(home, resident, {"title": "Fence", "category": "fence", "description": "x"}, [])


def test_stale_household_reply_does_not_undo_approval(req, board_user, resident):
    services.board_set_status(req, board_user, S.NEEDS_INFO, "Color?")
    stale = ExteriorRequest.objects.get(pk=req.pk)  # household's page loaded now
    services.board_set_status(req, board_user, S.APPROVED, "Fine as is")
    services.comment(stale, resident, "It's cedar")
    req.refresh_from_db()
    assert req.status == S.APPROVED and req.decided_by == board_user


def test_photos_on_board_only_notes_hidden_from_household(login, req, board_user, resident):
    login(board_user).post(
        reverse("exterior:comment", args=[req.pk]),
        {"note": "evidence", "board_only": "on", "photos": [png()]},
    )
    photo = RequestPhoto.objects.get()
    assert photo.board_only
    client = login(resident)
    assert list(client.get(req.get_absolute_url()).context["photos"]) == []
    assert client.get(reverse("exterior:photo", args=[photo.pk])).status_code == 403
    assert login(board_user).get(reverse("exterior:photo", args=[photo.pk])).status_code == 200


def test_board_member_cannot_decide_own_home(req, board_user, home):
    Residency.objects.create(home=home, user=board_user)
    with pytest.raises(ValueError, match="another board member"):
        services.board_set_status(req, board_user, S.APPROVED, "")


def test_board_only_note_by_board_household_keeps_needs_info(req, board_user, admin_user, home):
    services.board_set_status(req, admin_user, S.NEEDS_INFO, "Color?")
    Residency.objects.create(home=home, user=board_user)
    services.comment(req, board_user, "internal", board_only=True)
    req.refresh_from_db()
    assert req.status == S.NEEDS_INFO


def test_blank_status_gives_friendly_error(req, board_user):
    with pytest.raises(ValueError, match="Choose a new status"):
        services.board_set_status(req, board_user, "", "")


def test_submitter_on_board_not_emailed_about_own_request(
    board_user, admin_user, home, django_capture_on_commit_callbacks
):
    Residency.objects.create(home=home, user=board_user)
    with django_capture_on_commit_callbacks(execute=True):
        services.submit(home, board_user, {"title": "Shed", "category": "structure", "description": "x"}, [])
    assert [m.to for m in mail.outbox] == [[admin_user.email]]


def test_resident_submission_and_comment_limits(req, resident, home):
    for _ in range(services.SUBMISSIONS_PER_DAY - 1):
        services.submit(home, resident, {"title": "x", "category": "other", "description": "x"}, [])
    with pytest.raises(ValueError):
        services.submit(home, resident, {"title": "x", "category": "other", "description": "x"}, [])
    for _ in range(services.COMMENTS_PER_HOUR):
        services.comment(req, resident, "hi")
    with pytest.raises(ValueError):
        services.comment(req, resident, "hi")


def test_uploaded_photos_lose_exif(login, resident):
    upload = SimpleUploadedFile("gps.jpg", jpeg_with_gps(), content_type="image/jpeg")
    login(resident).post(
        reverse("exterior:new"),
        {"title": "Deck", "category": "structure", "description": "x", "photos": [upload]},
    )
    photo = RequestPhoto.objects.get()
    with photo.image.open("rb") as f:
        data = f.read()
    assert b"SecretPhone" not in data
    assert not Image.open(BytesIO(data)).getexif()


def test_image_bomb_rejected(login, resident, monkeypatch):
    from apps.core import uploads

    monkeypatch.setattr(uploads, "MAX_IMAGE_PIXELS", 10)
    response = login(resident).post(
        reverse("exterior:new"), {"title": "x", "category": "other", "description": "x", "photos": [png()]}
    )
    assert response.status_code == 200 and not ExteriorRequest.objects.exists()


class TestNotices:
    @pytest.fixture
    def notice(self, home, board_user):
        return CourtesyNotice.objects.create(home=home, subject="Cans", body="Please", created_by=board_user)

    def test_board_reply_emails_household(
        self, notice, board_user, resident, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            messaging.reply_to_notice(notice, board_user, "The blue car, thanks!")
        assert mail.outbox[-1].to == [resident.email] and "blue car" in mail.outbox[-1].body

    def test_notices_per_home_capped(self, login, board_user, resident, home):
        client = login(board_user)
        for i in range(4):
            client.post(
                reverse("messaging:notice_create"),
                {"home": home.pk, "subject": f"N{i}", "body": "b", "send_email": "on"},
            )
        assert len(mail.outbox) == 3 and CourtesyNotice.objects.count() == 4

    def test_invalid_reply_explains(self, login, notice, resident):
        response = login(resident).post(
            reverse("messaging:notice_reply", args=[notice.pk]), {"body": ""}, follow=True
        )
        assert "Please write a reply" in response.content.decode()


def test_message_committed_during_render_stays_unread(board_user, admin_user):
    thread = messaging.start_thread(board_user, "Topic", "first")
    first = thread.messages.get()
    messaging.mark_read(thread, admin_user, first.created_at)  # admin saw only the first message
    messaging.post_message(thread, board_user, "second")
    assert messaging.unread_count(admin_user) == 1


def test_mark_read_never_moves_backwards(board_user):
    thread = messaging.start_thread(board_user, "Topic", "first")
    before = BoardThread.objects.get(pk=thread.pk).last_activity_at
    messaging.mark_read(thread, board_user, before.replace(year=2000))
    assert messaging.unread_count(board_user) == 0
