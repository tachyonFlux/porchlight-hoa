import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.community.models import Residency
from apps.exterior import services
from apps.exterior.models import ExteriorRequest, RequestEvent, RequestPhoto

pytestmark = pytest.mark.django_db
S = ExteriorRequest.Status


def _real_png():
    from io import BytesIO

    from PIL import Image

    out = BytesIO()
    Image.new("RGB", (4, 4), "teal").save(out, "PNG")
    return out.getvalue()


PNG = _real_png()


def png(name="p.png"):
    return SimpleUploadedFile(name, PNG, content_type="image/png")


@pytest.fixture
def req(resident, home, board_user, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        return services.submit(
            home, resident, {"title": "New fence", "category": "fence", "description": "Cedar, 6ft"}, [png()]
        )


class TestSubmit:
    def test_submit_via_view_emails_board(
        self, login, resident, board_user, django_capture_on_commit_callbacks
    ):
        client = login(resident)
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                reverse("exterior:new"),
                {
                    "title": "Paint door blue",
                    "category": "paint",
                    "description": "Navy",
                    "photos": [png("a.png"), png("b.png")],
                },
            )
        req = ExteriorRequest.objects.get()
        assert response.url == req.get_absolute_url()
        assert req.photos.count() == 2 and req.status == S.SUBMITTED
        assert [m.to for m in mail.outbox] == [[board_user.email]]

    def test_only_households_can_submit(self, login, make_user):
        assert login(make_user("nobody@example.com")).get(reverse("exterior:new")).status_code == 403

    def test_fake_image_rejected(self, login, resident):
        bad = SimpleUploadedFile("x.png", b"<svg onload=alert(1)>", content_type="image/png")
        response = login(resident).post(
            reverse("exterior:new"), {"title": "X", "category": "other", "description": "Y", "photos": [bad]}
        )
        assert response.status_code == 200 and not ExteriorRequest.objects.exists()

    def test_svg_not_allowed(self, login, resident):
        svg = SimpleUploadedFile("x.svg", b"<svg/>", content_type="image/svg+xml")
        response = login(resident).post(
            reverse("exterior:new"), {"title": "X", "category": "other", "description": "Y", "photos": [svg]}
        )
        assert response.status_code == 200 and not ExteriorRequest.objects.exists()

    def test_photo_limit(self, req, resident):
        with pytest.raises(ValueError):
            services.add_photos(req, resident, [png() for _ in range(10)])


class TestAccess:
    def test_household_board_and_outsiders(self, login, req, resident, neighbor, board_user):
        assert login(resident).get(req.get_absolute_url()).status_code == 200
        assert login(board_user).get(req.get_absolute_url()).status_code == 200
        client = login(neighbor)
        assert client.get(req.get_absolute_url()).status_code == 403
        photo = req.photos.first()
        assert client.get(reverse("exterior:photo", args=[photo.pk])).status_code == 403

    def test_photo_served_privately(self, login, req, resident):
        photo = req.photos.first()
        assert photo.image.name.startswith("private/exterior/")
        response = login(resident).get(reverse("exterior:photo", args=[photo.pk]))
        assert response["Content-Type"] == "image/png" and response["X-Content-Type-Options"] == "nosniff"

    def test_former_resident_loses_access(self, login, req, resident):
        from django.utils import timezone

        Residency.objects.filter(user=resident).update(ended_on=timezone.localdate())
        assert login(resident).get(req.get_absolute_url()).status_code == 403

    def test_list_scopes(self, login, req, neighbor, board_user):
        assert list(login(neighbor).get(reverse("exterior:list")).context["reqs"]) == []
        assert list(login(board_user).get(reverse("exterior:list")).context["reqs"]) == [req]


class TestWorkflow:
    def test_needs_info_then_reply_returns_to_review(
        self, login, req, resident, board_user, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            login(board_user).post(
                reverse("exterior:status", args=[req.pk]),
                {"status": "needs_info", "note": "What color stain?"},
            )
        req.refresh_from_db()
        assert req.status == S.NEEDS_INFO
        assert mail.outbox[-1].to == [resident.email] and "What color stain?" in mail.outbox[-1].body
        with django_capture_on_commit_callbacks(execute=True):
            login(resident).post(reverse("exterior:comment", args=[req.pk]), {"note": "Natural cedar"})
        req.refresh_from_db()
        assert req.status == S.SUBMITTED
        assert mail.outbox[-1].to == [board_user.email]

    def test_needs_info_requires_note(self, req, board_user):
        with pytest.raises(ValueError):
            services.board_set_status(req, board_user, S.NEEDS_INFO, " ")

    def test_approve_records_decision_and_history(self, req, board_user):
        services.board_set_status(req, board_user, S.APPROVED, "Approved. Keep it under 6ft.")
        req.refresh_from_db()
        assert req.decided_by == board_user and req.decided_at and "6ft" in req.decision_note
        kinds = list(req.events.values_list("kind", "to_status"))
        assert kinds == [("submitted", "submitted"), ("status", "approved")]

    def test_invalid_transition(self, req, board_user, resident):
        services.withdraw(req, resident)
        with pytest.raises(ValueError):
            services.board_set_status(req, board_user, S.APPROVED, "")

    def test_reopen_clears_decision(self, req, board_user):
        services.board_set_status(req, board_user, S.DENIED, "Too tall")
        services.board_set_status(req, board_user, S.SUBMITTED, "Reconsidering")
        req.refresh_from_db()
        assert req.status == S.SUBMITTED and req.decided_at is None

    def test_resident_cannot_change_status(self, login, req, resident):
        assert (
            login(resident)
            .post(reverse("exterior:status", args=[req.pk]), {"status": "approved"})
            .status_code
            == 403
        )
        req.refresh_from_db()
        assert req.status == S.SUBMITTED

    def test_withdraw_only_by_household_and_while_open(self, login, req, resident, board_user, neighbor):
        assert login(neighbor).post(reverse("exterior:withdraw", args=[req.pk])).status_code == 403
        services.board_set_status(req, board_user, S.APPROVED, "")
        with pytest.raises(ValueError):
            services.withdraw(req, resident)

    def test_board_only_notes_hidden_from_household(self, login, req, resident, board_user):
        login(board_user).post(
            reverse("exterior:comment", args=[req.pk]),
            {"note": "Neighbor complained before", "board_only": "on"},
        )
        assert RequestEvent.objects.filter(board_only=True).exists()
        page = login(resident).get(req.get_absolute_url()).content.decode()
        assert "Neighbor complained" not in page
        assert "Neighbor complained" in login(board_user).get(req.get_absolute_url()).content.decode()

    def test_resident_cannot_post_board_only(self, login, req, resident):
        login(resident).post(reverse("exterior:comment", args=[req.pk]), {"note": "hi", "board_only": "on"})
        assert not RequestEvent.objects.filter(board_only=True).exists()

    def test_comment_with_photos(self, login, req, resident):
        login(resident).post(
            reverse("exterior:comment", args=[req.pk]), {"note": "sketch", "photos": [png()]}
        )
        assert RequestPhoto.objects.filter(request=req).count() == 2
