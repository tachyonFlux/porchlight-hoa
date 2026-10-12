from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.documents import services
from apps.documents.models import Document, DocumentCategory, ShareLink, ShareLinkAccess

pytestmark = pytest.mark.django_db


def pdf(name="bylaws.pdf", body=b"%PDF-1.4 bylaws"):
    return SimpleUploadedFile(name, body, content_type="application/pdf")


@pytest.fixture
def docs(board_user):
    cat = DocumentCategory.objects.get(name="Governing documents")
    return {
        "public": Document.objects.create(title="Bylaws", category=cat, file=pdf(), uploaded_by=board_user),
        "board": Document.objects.create(
            title="Legal memo", category=cat, file=pdf("memo.pdf"), visibility="board", uploaded_by=board_user
        ),
    }


def test_default_categories_seeded():
    assert DocumentCategory.objects.filter(name="Meeting minutes").exists()


class TestLibrary:
    def test_upload(self, login, board_user):
        cat = DocumentCategory.objects.first()
        response = login(board_user).post(
            reverse("documents:create"),
            {
                "title": "Budget 2027",
                "category": cat.pk,
                "visibility": "residents",
                "description": "",
                "file": pdf("budget.pdf"),
            },
        )
        assert response.status_code == 302
        doc = Document.objects.get()
        assert doc.original_name == "budget.pdf" and doc.file_size > 0
        assert doc.file.name.startswith("private/documents/")

    def test_rejects_disguised_file(self, login, board_user):
        response = login(board_user).post(
            reverse("documents:create"),
            {"title": "X", "visibility": "residents", "file": pdf("x.pdf", b"<html>")},
        )
        assert response.status_code == 200 and not Document.objects.exists()

    def test_visibility(self, login, docs, resident, board_user, make_user):
        client = login(resident)
        listed = [d for _, items in client.get(reverse("documents:list")).context["groups"] for d in items]
        assert listed == [docs["public"]]
        assert client.get(reverse("documents:download", args=[docs["public"].pk])).status_code == 200
        assert client.get(reverse("documents:download", args=[docs["board"].pk])).status_code == 403
        assert (
            login(board_user).get(reverse("documents:download", args=[docs["board"].pk])).status_code == 200
        )
        outsider = login(make_user("out@example.com"))
        assert outsider.get(reverse("documents:download", args=[docs["public"].pk])).status_code == 403

    def test_download_name_and_headers(self, login, docs, resident):
        response = login(resident).get(reverse("documents:download", args=[docs["public"].pk]))
        assert 'filename="Bylaws.pdf"' in response["Content-Disposition"]  # title + extension
        assert response["X-Content-Type-Options"] == "nosniff"

    def test_replace_and_delete_remove_files(
        self, login, board_user, docs, django_capture_on_commit_callbacks
    ):
        doc = docs["public"]
        storage, first = doc.file.storage, doc.file.name
        client = login(board_user)
        with django_capture_on_commit_callbacks(execute=True):
            client.post(
                reverse("documents:edit", args=[doc.pk]),
                {
                    "title": doc.title,
                    "category": doc.category_id,
                    "visibility": "residents",
                    "description": "",
                    "file": pdf("bylaws-2027.pdf"),
                },
            )
        doc.refresh_from_db()
        assert not storage.exists(first) and doc.original_name == "bylaws-2027.pdf"
        second = doc.file.name
        with django_capture_on_commit_callbacks(execute=True):
            client.post(reverse("documents:edit", args=[doc.pk]), {"delete": "1"})
        assert not storage.exists(second) and not Document.objects.filter(pk=doc.pk).exists()

    def test_residents_cannot_upload(self, login, resident):
        assert login(resident).get(reverse("documents:create")).status_code == 403


class TestShareLinks:
    def make_link(self, client, docs, **extra):
        data = {"label": "Smith closing", "documents": [docs["public"].pk], "expires_in": "7"}
        data.update(extra)
        client.post(reverse("documents:share_create"), data)
        url = client.get(reverse("documents:share_list")).context["new_link"]["url"]
        return url.replace("http://testserver", "")

    def test_full_flow(self, login, client, board_user, docs):
        board = login(board_user)
        path = self.make_link(board, docs)
        link = ShareLink.objects.get()
        assert path.split("/")[2] not in link.token_hash  # only the hash is stored
        assert board.get(reverse("documents:share_list")).context["new_link"] is None  # shown once
        board.logout()
        page = client.get(path)
        assert page.status_code == 200 and page["Cache-Control"] == "private, no-store"
        assert "noindex" in page["X-Robots-Tag"] and page["Referrer-Policy"] == "no-referrer"
        download = client.get(f"{path}{docs['public'].pk}/")
        assert download.status_code == 200
        assert b"".join(download.streaming_content) == b"%PDF-1.4 bylaws"
        assert client.get(f"{path}{docs['board'].pk}/").status_code == 404  # not on this link
        link.refresh_from_db()
        assert link.view_count == 1
        assert ShareLinkAccess.objects.filter(link=link, document=docs["public"]).count() == 1

    def test_max_downloads(self, login, client, board_user, docs):
        path = self.make_link(login(board_user), docs, max_views="2")
        client.logout()
        codes = [client.get(f"{path}{docs['public'].pk}/").status_code for _ in range(3)]
        assert codes == [200, 200, 410]
        assert client.get(path).status_code == 410
        assert ShareLinkAccess.objects.filter(allowed=False).exists()

    def test_expiry_and_revoke(self, login, client, board_user, docs):
        board = login(board_user)
        path = self.make_link(board, docs)
        link = ShareLink.objects.get()
        ShareLink.objects.filter(pk=link.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        client.logout()
        assert client.get(path).status_code == 410
        ShareLink.objects.filter(pk=link.pk).update(expires_at=timezone.now() + timedelta(days=1))
        assert client.get(path).status_code == 200
        login(board_user).post(reverse("documents:share_revoke", args=[link.pk]))
        client.logout()
        assert client.get(path).status_code == 410
        assert client.get(f"{path}{docs['public'].pk}/").status_code == 410

    def test_bad_tokens_rate_limited(self, client):
        codes = [client.get(reverse("documents:shared", args=[f"bad{i}"])).status_code for i in range(31)]
        assert codes[:30] == [404] * 30 and codes[30] == 429

    def test_claim_download_exact_limit(self, board_user, docs):
        link, _ = services.create_share_link(
            board_user, "x", [docs["public"]], timezone.now() + timedelta(days=1), 1
        )
        assert services.claim_download(link) is True
        assert services.claim_download(link) is False

    def test_board_only(self, login, resident):
        client = login(resident)
        assert client.get(reverse("documents:share_list")).status_code == 403
        assert client.get(reverse("documents:share_create")).status_code == 403
