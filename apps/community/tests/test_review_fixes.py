"""Regression tests for issues found in the slice-1 cross review."""

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.community import services
from apps.community.models import ClaimToken, Home, HomeInvite, Residency

pytestmark = pytest.mark.django_db
User = get_user_model()


def _csv(text):
    return SimpleUploadedFile("x.csv", text.encode())


def test_board_search_does_not_multiply_resident_counts(login, board_user, home, make_user):
    for i in range(3):
        Residency.objects.create(home=home, user=make_user(f"smith{i}@example.com", full_name="Pat Smith"))
    for q in ("smith", "Elm"):
        response = login(board_user).get(reverse("community:board_homes"), {"q": q})
        assert [h.resident_count for h in response.context["homes"]] == [3]


def test_new_home_gets_invite_immediately():
    home = Home.objects.create(address="1 New Lane")
    assert HomeInvite.objects.valid().filter(home=home).count() == 1


class TestInactiveUsers:
    @pytest.fixture
    def inactive(self, resident):
        resident.is_active = False
        resident.save()
        return resident

    def test_attach_raises(self, inactive, home):
        with pytest.raises(services.InactiveUserError):
            services.attach_resident(home, inactive.email)

    def test_import_flags_row_instead_of_crashing(self, inactive):
        rows, _ = services.parse_residents_csv(_csv(f"address,email\n99 Oak,{inactive.email}\n"))
        assert not rows[0].ok and "deactivated" in rows[0].errors[0]

    def test_claim_by_inactive_user_rejected_cleanly(self, client, inactive, other_home):
        invite = HomeInvite.active_for(other_home)
        _, raw = ClaimToken.issue(invite, inactive.email)
        response = client.post(reverse("community:claim_confirm", args=[raw]))
        assert response.status_code == 410
        assert "_auth_user_id" not in client.session


class TestInviteRotation:
    def test_code_rotates_after_claim(self, client, home):
        invite = HomeInvite.active_for(home)
        _, raw = ClaimToken.issue(invite, "first@example.com")
        client.post(reverse("community:claim_confirm", args=[raw]))
        assert HomeInvite.lookup(invite.code) is None
        new = HomeInvite.active_for(home)
        assert new.code != invite.code

    def test_signed_in_reuse_of_old_code_blocked(self, login, neighbor, home):
        invite = HomeInvite.active_for(home)
        services.complete_claim(ClaimToken(invite=invite, email="first@example.com"))
        client = login(neighbor)
        response = client.post(reverse("community:claim_code", args=[invite.code]), {"relation": "owner"})
        assert response.status_code == 404
        assert not Residency.objects.filter(user=neighbor, home=home).exists()

    def test_household_sees_current_code(self, login, resident, home):
        response = login(resident).get(home.get_absolute_url())
        assert response.context["invite"] == HomeInvite.active_for(home)

    def test_household_and_board_notified(self, client, resident, home, hoa):
        hoa.contact_email = "board@example.com"
        hoa.save()
        _, raw = ClaimToken.issue(HomeInvite.active_for(home), "newcomer@example.com")
        client.post(reverse("community:claim_confirm", args=[raw]))
        notice = [m for m in mail.outbox if "joined" in m.subject][0]
        assert sorted(notice.to) == ["board@example.com", "resident@example.com"]


class TestClaimAbuse:
    def test_requester_name_not_in_email(self, client, home):
        invite = HomeInvite.active_for(home)
        client.post(
            reverse("community:claim_code", args=[invite.code]),
            {"full_name": "Click http://evil.example now", "email": "x@example.com", "relation": "owner"},
        )
        assert "evil.example" not in mail.outbox[0].body

    def test_per_email_limit(self, client, home, other_home):
        for target in (home, other_home, home, other_home):
            invite = HomeInvite.active_for(target)
            response = client.post(
                reverse("community:claim_code", args=[invite.code]),
                {"full_name": "A", "email": "victim@example.com", "relation": "owner"},
            )
        assert response.status_code == 429
        assert len(mail.outbox) == 3


class TestCsvHeadersAndNames:
    def test_loose_headers(self):
        rows, warnings = services.parse_residents_csv(
            _csv(
                "Property Address,Lot #,Owner Name(s),E-mail Address,Home Phone\n"
                "1 Elm,3,Al Bo,al@example.com,555\n"
            )
        )
        person = rows[0].people[0]
        assert (rows[0].lot, person.name, person.email, person.phone) == (
            "3",
            "Al Bo",
            "al@example.com",
            "555",
        )
        assert warnings == []

    def test_warns_without_email_column(self):
        _, warnings = services.parse_residents_csv(_csv("address,owner\n1 Elm,Al\n"))
        assert "No email column" in warnings[0]

    def test_comma_separated_names_and_phones_pair_up(self):
        rows, _ = services.parse_residents_csv(
            _csv(
                "address,names,email,phone\n"
                '1 Elm,"John Smith, Jane Smith","j@example.com; k@example.com","555-1, 555-2"\n'
            )
        )
        assert [(p.name, p.phone) for p in rows[0].people] == [
            ("John Smith", "555-1"),
            ("Jane Smith", "555-2"),
        ]

    def test_count_mismatch_warns(self):
        rows, _ = services.parse_residents_csv(_csv('address,names,email\n1 Elm,"A;B;C",a@example.com\n'))
        assert rows[0].warnings

    def test_line_numbers_follow_multiline_cells(self):
        rows, _ = services.parse_residents_csv(_csv('address,names\n"1 Elm","A\nB"\n2 Elm,C\n'))
        assert [r.line for r in rows] == [3, 4]


def test_ending_primary_promotes_next_resident(login, board_user, resident, home, make_user):
    other = make_user("second@example.com")
    Residency.objects.create(home=home, user=other)
    primary = Residency.objects.get(user=resident)
    login(board_user).post(reverse("community:residency_end", args=[primary.pk]))
    assert Residency.objects.get(user=other, home=home).is_primary_contact


def test_home_page_query_count_does_not_grow_with_details(
    login, resident, home, django_assert_max_num_queries
):
    from apps.community.models import HomeDetail

    for i in range(20):
        HomeDetail.objects.create(home=home, key=f"k{i}", value="v")
    client = login(resident)
    client.get(home.get_absolute_url())  # warm up session/settings rows
    with django_assert_max_num_queries(20):
        client.get(home.get_absolute_url())
