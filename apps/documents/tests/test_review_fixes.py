"""Regression tests for issues found in the slice-5 cross review."""

from datetime import date, timedelta

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import BoardPosition
from apps.compliance import services as compliance
from apps.compliance.models import Deadline
from apps.documents import services
from apps.documents.models import Document, DocumentCategory, ShareLink, ShareLinkAccess

pytestmark = pytest.mark.django_db


def pdf(name="a.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4 x", content_type="application/pdf")


@pytest.fixture
def docs(board_user):
    return {
        "public": Document.objects.create(title="Bylaws", file=pdf(), uploaded_by=board_user),
        "board": Document.objects.create(
            title="Legal memo", file=pdf(), visibility="board", uploaded_by=board_user
        ),
    }


class TestCompliance:
    def test_former_board_owner_falls_back_to_board(self, board_user, admin_user):
        d = Deadline.objects.create(
            title="Report", due_date=date(2027, 3, 2), remind_days="1", owner=board_user
        )
        BoardPosition.objects.filter(user=board_user).update(ended_on=timezone.localdate())
        compliance.send_reminders(date(2027, 3, 1))
        assert [m.to for m in mail.outbox] == [[admin_user.email]]
        from apps.compliance.forms import DeadlineForm

        form = DeadlineForm(instance=d)
        assert board_user in form.fields["owner"].queryset  # form still saves

    def test_reopen_keeps_edited_next_occurrence(self, board_user):
        d = Deadline.objects.create(title="Report", due_date=timezone.localdate() + timedelta(days=10))
        nxt = compliance.complete(d, board_user)
        Deadline.objects.filter(pk=nxt.pk).update(description="Use the new SDAT portal")
        compliance.reopen(d, board_user)
        assert Deadline.objects.filter(pk=nxt.pk).exists()
        again = compliance.complete(d, board_user)
        assert again.pk == nxt.pk and Deadline.objects.count() == 2

    def test_reopen_then_complete_does_not_duplicate_reminded_item(self, board_user):
        d = Deadline.objects.create(title="Report", due_date=timezone.localdate() + timedelta(days=10))
        nxt = compliance.complete(d, board_user)
        nxt.reminders.create(days_before=30)
        compliance.reopen(d, board_user)
        compliance.complete(d, board_user)
        assert Deadline.objects.filter(completed_at__isnull=True).count() == 1

    def test_very_late_completion_schedules_future_date(self, board_user):
        d = Deadline.objects.create(title="Report", due_date=timezone.localdate() - timedelta(days=400))
        nxt = compliance.complete(d, board_user)
        assert nxt.due_date >= timezone.localdate()

    def test_no_recipients_not_marked_sent(self):
        Deadline.objects.create(title="Report", due_date=date(2027, 3, 2), remind_days="1")
        compliance.send_reminders(date(2027, 3, 1))
        assert not Deadline.objects.get().reminders.exists()


class TestShareLinks:
    def test_non_admin_cannot_share_board_only(self, login, board_user, docs):
        response = login(board_user).post(
            reverse("documents:share_create"),
            {"label": "x", "documents": [docs["board"].pk], "expires_in": "7"},
        )
        assert response.status_code == 200 and not ShareLink.objects.exists()
        assert "BOARD ONLY" in response.content.decode()

    def test_admin_can_share_board_only_and_it_is_audited(self, login, admin_user, docs):
        from apps.core.models import AuditLog

        login(admin_user).post(
            reverse("documents:share_create"),
            {"label": "x", "documents": [docs["board"].pk], "expires_in": "7"},
        )
        link = ShareLink.objects.get()
        assert link.allow_board_only
        assert "Board only" in str(AuditLog.objects.get(action="documents.share_link_created").changes)

    def test_doc_made_board_only_later_is_withheld(self, client, board_user, docs):
        link, raw = services.create_share_link(
            board_user, "x", [docs["public"]], timezone.now() + timedelta(days=1)
        )
        Document.objects.filter(pk=docs["public"].pk).update(visibility="board")
        assert (
            client.get(reverse("documents:shared_download", args=[raw, docs["public"].pk])).status_code == 404
        )
        assert client.get(reverse("documents:shared", args=[raw])).context["docs"] == []

    def test_label_not_public_and_downloads_are_attachments(self, client, board_user, docs):
        link, raw = services.create_share_link(
            board_user, "Smith closing", [docs["public"]], timezone.now() + timedelta(days=1)
        )
        assert b"Smith" not in client.get(reverse("documents:shared", args=[raw])).content
        response = client.get(reverse("documents:shared_download", args=[raw, docs["public"].pk]))
        assert response["Content-Disposition"].startswith("attachment")

    def test_per_visitor_limit(self, client, board_user, docs):
        link, raw = services.create_share_link(
            board_user, "x", [docs["public"]], timezone.now() + timedelta(days=1)
        )
        codes = [client.get(reverse("documents:shared", args=[raw])).status_code for _ in range(61)]
        assert codes[-1] == 429 and ShareLinkAccess.objects.count() == 60

    def test_access_log_keeps_title_after_delete(self, client, board_user, docs):
        link, raw = services.create_share_link(
            board_user, "x", [docs["public"]], timezone.now() + timedelta(days=1)
        )
        client.get(reverse("documents:shared_download", args=[raw, docs["public"].pk]))
        docs["public"].delete()
        assert ShareLinkAccess.objects.get().document_title == "Bylaws"

    def test_old_access_logs_purged(self, client, board_user, docs):
        link, raw = services.create_share_link(
            board_user, "x", [docs["public"]], timezone.now() + timedelta(days=1)
        )
        client.get(reverse("documents:shared", args=[raw]))
        ShareLink.objects.filter(pk=link.pk).update(expires_at=timezone.now() - timedelta(days=91))
        assert services.purge_access_logs(timezone.now()) == 1


class TestLibrary:
    def test_uncategorized_listed_last(self, login, board_user):
        Document.objects.create(title="Loose", file=pdf())
        Document.objects.create(title="Bylaws", file=pdf(), category=DocumentCategory.objects.first())
        groups = login(board_user).get(reverse("documents:list")).context["groups"]
        assert groups[-1][0] is None

    def test_category_rename_and_case_insensitive_unique(self, login, board_user):
        cat = DocumentCategory.objects.get(name="Forms")
        client = login(board_user)
        client.post(
            reverse("documents:categories"), {"edit": cat.pk, "name": "Forms & applications", "sort_order": 5}
        )
        cat.refresh_from_db()
        assert cat.name == "Forms & applications" and cat.sort_order == 5
        client.post(reverse("documents:categories"), {"name": "insurance", "sort_order": 1})
        assert DocumentCategory.objects.filter(name__iexact="insurance").count() == 1

    def test_document_images_lose_exif(self, login, board_user):
        from io import BytesIO

        from PIL import Image

        img = Image.new("RGB", (4, 4))
        exif = Image.Exif()
        exif[0x0110] = "SecretPhone"
        out = BytesIO()
        img.save(out, "JPEG", exif=exif.tobytes())
        login(board_user).post(
            reverse("documents:create"),
            {
                "title": "Site photo",
                "visibility": "residents",
                "file": SimpleUploadedFile("p.jpg", out.getvalue(), content_type="image/jpeg"),
            },
        )
        with Document.objects.get().file.open("rb") as f:
            assert b"SecretPhone" not in f.read()
