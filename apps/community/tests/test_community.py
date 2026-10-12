import re

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.community import services
from apps.community.models import ClaimToken, Home, HomeDetail, HomeInvite, Residency

pytestmark = pytest.mark.django_db
User = get_user_model()
V = HomeDetail.Visibility


# ------------------------------------------------------------------ visibility


@pytest.fixture
def details(home):
    return {
        level: HomeDetail.objects.create(home=home, key=f"{level} fact", value="x", visibility=level)
        for level in (V.HOUSEHOLD, V.NEIGHBORS, V.BOARD)
    }


class TestVisibility:
    def keys(self, user, home):
        return {d.visibility for d in services.visible_details(user, home)}

    def test_household_sees_household_and_neighbors(self, resident, home, details):
        assert self.keys(resident, home) == {V.HOUSEHOLD, V.NEIGHBORS}

    def test_neighbor_sees_only_neighbors(self, neighbor, home, details):
        assert self.keys(neighbor, home) == {V.NEIGHBORS}

    def test_board_sees_all(self, board_user, home, details):
        assert self.keys(board_user, home) == {V.HOUSEHOLD, V.NEIGHBORS, V.BOARD}

    def test_unlinked_user_sees_nothing(self, make_user, home, details):
        assert self.keys(make_user("loner@example.com"), home) == set()

    def test_former_resident_loses_household_access(self, resident, home, details):
        Residency.objects.filter(user=resident).update(ended_on=timezone.localdate())
        assert self.keys(resident, home) == set()

    def test_home_page_respects_visibility(self, login, neighbor, home, details):
        response = login(neighbor).get(home.get_absolute_url())
        assert response.status_code == 200
        content = response.content.decode()
        assert "neighbors fact" in content
        assert "household fact" not in content and "board fact" not in content
        assert "Rae Resident" not in content  # neighbors don't see who lives there

    def test_unlinked_user_cannot_open_home_or_directory(self, login, make_user, home):
        client = login(make_user("loner@example.com"))
        assert client.get(home.get_absolute_url()).status_code == 403
        assert client.get(reverse("community:directory")).status_code == 403

    def test_household_cannot_edit_board_detail(self, login, resident, details):
        client = login(resident)
        url = reverse("community:detail_edit", args=[details[V.BOARD].pk])
        assert client.get(url).status_code == 403

    def test_household_cannot_create_board_only_detail(self, login, resident, home):
        client = login(resident)
        response = client.post(
            reverse("community:detail_add", args=[home.pk]),
            {"key": "Secret", "value": "x", "visibility": V.BOARD},
        )
        assert response.status_code == 200  # form error: choice not allowed
        assert not HomeDetail.objects.exists()

    def test_household_adds_detail(self, login, resident, home):
        client = login(resident)
        response = client.post(
            reverse("community:detail_add", args=[home.pk]),
            {"key": "Dog", "value": "Biscuit", "visibility": V.NEIGHBORS},
        )
        assert response.status_code == 302
        assert HomeDetail.objects.get().updated_by == resident

    def test_neighbor_cannot_add_detail(self, login, neighbor, home):
        response = login(neighbor).get(reverse("community:detail_add", args=[home.pk]))
        assert response.status_code == 403


# ------------------------------------------------------------------ claim flow


def _confirm_path():
    return re.search(r"http://testserver(/claim/confirm/[^\s]+)", mail.outbox[-1].body).group(1)


class TestClaimFlow:
    def test_full_claim(self, client, home):
        invite = HomeInvite.active_for(home)
        # Residents may type the code in lower case with a dash.
        code = f"{invite.code[:4].lower()}-{invite.code[4:].lower()}"
        response = client.post(reverse("community:claim_start"), {"code": code})
        assert response.url == reverse("community:claim_code", args=[invite.code])

        response = client.post(
            response.url,
            {"full_name": "Cleo Claimer", "email": "Cleo@Example.com", "relation": "tenant"},
        )
        assert response.url == reverse("community:claim_sent")
        assert mail.outbox[-1].to == ["cleo@example.com"]
        assert not User.objects.filter(email="cleo@example.com").exists()  # not until confirmed

        path = _confirm_path()
        assert client.get(path).status_code == 200
        response = client.post(path)
        assert response.status_code == 302
        user = User.objects.get(email="cleo@example.com")
        assert user.full_name == "Cleo Claimer"
        residency = Residency.objects.get(user=user, home=home)
        assert residency.relation == "tenant" and residency.source == "claim"
        assert residency.is_primary_contact
        assert client.session["_auth_user_id"] == str(user.pk)
        invite.refresh_from_db()
        assert invite.use_count == 1

        client.logout()
        assert client.post(path).status_code == 410  # single use

    def test_bad_code(self, client):
        response = client.post(reverse("community:claim_start"), {"code": "NOPE-NOPE"})
        assert response.status_code == 200
        assert b"couldn" in response.content

    def test_revoked_invite_rejected(self, client, home):
        invite = HomeInvite.active_for(home)
        invite.revoke()
        assert client.get(reverse("community:claim_code", args=[invite.code])).status_code == 404

    def test_revoking_after_email_blocks_confirmation(self, client, home):
        invite = HomeInvite.active_for(home)
        _, raw = ClaimToken.issue(invite, "x@example.com")
        invite.revoke()
        assert client.post(reverse("community:claim_confirm", args=[raw])).status_code == 410

    def test_inactive_home_invite_rejected(self, client, home):
        invite = HomeInvite.active_for(home)
        home.is_active = False
        home.save()
        assert HomeInvite.lookup(invite.code) is None

    def test_existing_user_claim_does_not_duplicate(self, client, resident, home):
        invite = HomeInvite.active_for(home)
        _, raw = ClaimToken.issue(invite, resident.email, "Other Name")
        client.post(reverse("community:claim_confirm", args=[raw]))
        assert Residency.objects.filter(user=resident, home=home).count() == 1
        resident.refresh_from_db()
        assert resident.full_name == "Rae Resident"  # existing name not overwritten

    def test_signed_in_user_claims_directly(self, login, neighbor, home):
        client = login(neighbor)
        invite = HomeInvite.active_for(home)
        response = client.post(reverse("community:claim_code", args=[invite.code]), {"relation": "owner"})
        assert response.status_code == 302
        assert Residency.objects.current().filter(user=neighbor, home=home).exists()
        assert not mail.outbox

    def test_regenerate_invalidates_old_code(self, login, board_user, home):
        old = HomeInvite.active_for(home)
        login(board_user).post(reverse("community:invite_regenerate", args=[home.pk]))
        assert HomeInvite.lookup(old.code) is None
        assert HomeInvite.active_for(home).code != old.code

    def test_invite_letters_render(self, login, board_user, home, other_home):
        response = login(board_user).get(reverse("community:invite_letters"))
        assert response.status_code == 200
        assert len(response.context["letters"]) == 2
        assert b"<svg" in response.content


# ------------------------------------------------------------------ CSV import


def _csv(text, name="homes.csv"):
    return SimpleUploadedFile(name, text.encode("utf-8-sig"), content_type="text/csv")


SAMPLE = """Address,Lot #,Owner Name(s),Email,Phone
1 Oak Court,1,Pat Lee; Sam Lee,pat@example.com; sam@example.com,555-0101; 555-0102
2 Oak Court,2,Jo Park,JO@example.com,555-0103
3 Oak Court,3,,,
,4,No Address,na@example.com,
5 Oak Court,5,Bad Email,not-an-email,
2  Oak  Court,2,Dup,dup@example.com,
"""


class TestCsvImport:
    def test_parse(self):
        rows = services.parse_residents_csv(_csv(SAMPLE))[0]
        assert [r.ok for r in rows] == [True, True, True, False, False, False]
        first = rows[0]
        assert [(p.name, p.email, p.phone) for p in first.people] == [
            ("Pat Lee", "pat@example.com", "555-0101"),
            ("Sam Lee", "sam@example.com", "555-0102"),
        ]
        assert rows[1].people[0].email == "jo@example.com"
        assert "Missing address" in rows[3].errors[0]
        assert "Duplicate" in rows[5].errors[0]

    def test_apply_is_idempotent(self):
        rows = services.parse_residents_csv(_csv(SAMPLE))[0]
        stats = services.apply_import(rows)
        assert stats == {"homes_created": 3, "homes_updated": 0, "residents_linked": 3, "skipped": 3}
        again = services.apply_import(services.parse_residents_csv(_csv(SAMPLE))[0])
        assert again["homes_created"] == 0 and again["residents_linked"] == 0
        assert Home.objects.count() == 3
        assert User.objects.count() == 3
        assert Residency.objects.filter(home__address="1 Oak Court").count() == 2

    def test_single_name_shared_by_multiple_emails(self):
        rows, _ = services.parse_residents_csv(
            _csv("address,owners,emails\n9 Pine Way,The Garcia Family,a@example.com;b@example.com\n")
        )
        assert [p.name for p in rows[0].people] == ["The Garcia Family"] * 2

    def test_updates_lot_and_matches_case_insensitively(self, home):
        services.apply_import(services.parse_residents_csv(_csv("address,lot\n12 elm street,44\n"))[0])
        home.refresh_from_db()
        assert home.lot_number == "44"
        assert Home.objects.count() == 1

    def test_missing_address_column(self):
        with pytest.raises(services.CSVImportError):
            services.parse_residents_csv(_csv("name,email\nA,a@example.com\n"))

    def test_preview_then_confirm_view(self, login, board_user):
        client = login(board_user)
        response = client.post(reverse("community:import"), {"file": _csv(SAMPLE)})
        assert response.status_code == 200
        assert response.context["ok_count"] == 3
        assert not Home.objects.exists()  # preview writes nothing
        response = client.post(reverse("community:import"), {"confirm": "1"})
        assert response.status_code == 302
        assert Home.objects.count() == 3

    def test_rejects_non_csv(self, login, board_user):
        client = login(board_user)
        upload = SimpleUploadedFile("x.xlsx", b"PK\x03\x04", content_type="application/zip")
        response = client.post(reverse("community:import"), {"file": upload})
        assert response.status_code == 200
        assert "file" in response.context["form"].errors

    def test_latin1_fallback(self):
        f = SimpleUploadedFile("x.csv", "address\n7 Caf\xe9 Row\n".encode("latin-1"))
        rows, _ = services.parse_residents_csv(f)
        assert rows[0].address == "7 Café Row"


class TestBoardHomeManagement:
    def test_add_and_remove_resident(self, login, board_user, home):
        client = login(board_user)
        client.post(
            reverse("community:resident_add", args=[home.pk]),
            {"email": "new@example.com", "full_name": "Nia New", "phone": "", "relation": "owner"},
        )
        residency = Residency.objects.get(user__email="new@example.com")
        client.post(reverse("community:residency_end", args=[residency.pk]))
        residency.refresh_from_db()
        assert residency.ended_on is not None
        # The person can be re-added later (the unique constraint only covers current rows).
        client.post(
            reverse("community:resident_add", args=[home.pk]),
            {"email": "new@example.com", "full_name": "", "phone": "", "relation": "owner"},
        )
        assert Residency.objects.filter(user__email="new@example.com").count() == 2

    def test_duplicate_address_rejected(self, login, board_user, home):
        response = login(board_user).post(
            reverse("community:home_create"),
            {"address": "12 ELM STREET", "lot_number": "", "notes": "", "is_active": "on"},
        )
        assert response.status_code == 200
        assert Home.objects.count() == 1

    def test_board_search(self, login, board_user, resident, home, other_home):
        response = login(board_user).get(reverse("community:board_homes"), {"q": "Rae"})
        assert list(response.context["homes"]) == [home]
