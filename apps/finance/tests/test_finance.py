import base64
import json
from datetime import date
from pathlib import Path
from unittest import mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.dues import services as dues
from apps.finance import parsers, services, simplefin
from apps.finance.models import (
    Account,
    BankAccount,
    CategoryRule,
    PaymentMatch,
    SimpleFINAccountLink,
    SimpleFINConnection,
    StatementBalance,
    Transaction,
)

FIXTURES = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.django_db


def fixture(name):
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def bank(db):
    return BankAccount.objects.create(
        name="Operating", last4="1234", opening_balance_cents=1000000, opening_date=date(2027, 1, 1)
    )


def csv_rows(name="bank_amount_column.csv", **kw):
    headers, records = parsers.read_csv(parsers.decode(fixture(name)))
    return parsers.apply_mapping(headers, records, parsers.guess_mapping(headers), **kw)


# ------------------------------------------------------------------ parsing


class TestParsers:
    @pytest.mark.parametrize(
        "raw,cents",
        [
            ("300.00", 30000),
            ("$1,204.10", 120410),
            ("(12.50)", -1250),
            ("-850", -85000),
            ("12.00-", -1200),
            ("", None),
            ("+5", 500),
        ],
    )
    def test_amounts(self, raw, cents):
        assert parsers.parse_amount(raw) == cents

    def test_csv_with_preamble_and_amount_column(self):
        rows = csv_rows()
        good = [r for r in rows if r.ok]
        assert len(rows) == 7 and len(good) == 6
        assert good[0].date == date(2027, 1, 5) and good[0].amount_cents == 30000
        assert good[3].check_number == "1042" and good[3].amount_cents == -12050
        assert good[5].description == "DEPOSIT, BRANCH 12" and good[5].amount_cents == 60000
        assert "not-money" in rows[-1].errors[0]

    def test_identical_rows_get_distinct_but_stable_keys(self):
        first, second = csv_rows()[:2]
        assert first.dedupe_key != second.dedupe_key
        again = csv_rows()
        assert [r.dedupe_key for r in again[:2]] == [first.dedupe_key, second.dedupe_key]

    def test_split_debit_credit_columns(self):
        rows = csv_rows("bank_split_columns.csv")
        assert [r.amount_cents for r in rows] == [-85000, 127, -120410]
        assert rows[0].date == date(2027, 1, 12)

    def test_flip_sign(self):
        assert csv_rows(flip_sign=True)[0].amount_cents == -30000

    def test_guess_mapping(self):
        headers, _ = parsers.read_csv(parsers.decode(fixture("bank_split_columns.csv")))
        assert parsers.guess_mapping(headers) == {
            "date": "Posting Date",
            "description": "Transaction Description",
            "withdrawal": "Withdrawals",
            "deposit": "Deposits",
        }

    def test_no_header_row(self):
        with pytest.raises(parsers.ParseError):
            parsers.read_csv("a,b\n1,2\n")

    def test_ofx(self):
        last4, rows = parsers.parse_ofx(fixture("statement.ofx"))
        assert last4 == "1234" and [r.amount_cents for r in rows] == [60000, -85000, -12050]
        assert rows[2].check_number == "1042" and rows[0].dedupe_key.startswith("ofx:")

    def test_bad_ofx(self):
        with pytest.raises(parsers.ParseError):
            parsers.parse_ofx(b"OFXHEADER:100\n<OFX>garbage")


# ------------------------------------------------------------------ import & categorize


class TestImport:
    def test_overlapping_files_dedupe(self, bank, board_user):
        first = services.import_rows(csv_rows(), bank, "csv", "jan.csv", board_user)
        assert (first.rows_imported, first.rows_duplicate, first.rows_error) == (6, 0, 1)
        second = services.import_rows(csv_rows(), bank, "csv", "jan-again.csv", board_user)
        assert (second.rows_imported, second.rows_duplicate) == (0, 6)
        assert Transaction.objects.count() == 6

    def test_ofx_reimport_dedupes_by_fitid(self, bank):
        _, rows = parsers.parse_ofx(fixture("statement.ofx"))
        services.import_rows(rows, bank, "ofx")
        _, rows = parsers.parse_ofx(fixture("statement.ofx"))
        assert services.import_rows(rows, bank, "ofx").rows_imported == 0

    def test_rules_applied_on_import_in_priority_order(self, bank):
        lawn = Account.objects.get(code="5000")
        interest = Account.objects.get(code="4100")
        CategoryRule.objects.create(pattern="greenscape", account=lawn, priority=10)
        CategoryRule.objects.create(pattern="interest", account=interest, direction="in")
        CategoryRule.objects.create(pattern="payment", account=lawn, priority=200)  # lower priority
        services.import_rows(csv_rows(), bank, "csv")
        t = Transaction.objects.get(description__icontains="greenscape")
        assert t.account == lawn and t.categorized_by == "rule"
        assert Transaction.objects.get(description="INTEREST PAYMENT").account == interest

    def test_direction_rule(self):
        rule = CategoryRule(pattern="deposit", direction="out", account=Account.objects.first())
        assert not rule.matches("DEPOSIT", 100) and rule.matches("deposit", -100)

    def test_manual_override_and_make_rule(self, bank, board_user):
        services.import_rows(csv_rows(), bank, "csv")
        t = Transaction.objects.get(description__icontains="greenscape")
        lawn = Account.objects.get(code="5000")
        rule = services.set_category(t, lawn, board_user, make_rule=True, pattern="GREENSCAPE")
        t.refresh_from_db()
        assert t.categorized_by == "manual" and rule.direction == "out"

    def test_upload_views_csv_flow(self, login, board_user, bank):
        client = login(board_user)
        upload = SimpleUploadedFile("jan.csv", fixture("bank_amount_column.csv"))
        response = client.post(reverse("finance:import_upload"), {"bank_account": bank.pk, "file": upload})
        assert response.url == reverse("finance:import_map")
        form = client.get(response.url).context["form"]
        data = {
            f"col_{k}": v
            for k, v in parsers.guess_mapping(
                form.fields["col_date"].choices and [c[0] for c in form.fields["col_date"].choices if c[0]]
            ).items()
        }
        data.update({"preview": "1", "save_as": "Bank checking"})
        assert client.post(reverse("finance:import_map"), data).context["ok_count"] == 6
        data["confirm"] = "1"
        client.post(reverse("finance:import_map"), data)
        assert Transaction.objects.count() == 6
        assert "finance_pending_import" not in client.session  # temp file handled and forgotten

    def test_upload_view_ofx(self, login, board_user, bank):
        upload = SimpleUploadedFile("q1.qfx", fixture("statement.ofx"))
        response = login(board_user).post(
            reverse("finance:import_upload"), {"bank_account": bank.pk, "file": upload}
        )
        assert response.status_code == 302 and Transaction.objects.count() == 3

    def test_only_treasurer_imports(self, login, make_user, bank):
        from apps.accounts.models import BoardPosition

        secretary = make_user("sec@example.com")
        BoardPosition.objects.create(user=secretary, title="secretary")
        client = login(secretary)
        assert client.get(reverse("finance:import_upload")).status_code == 403
        assert client.get(reverse("finance:transactions")).status_code == 200  # can look

    def test_residents_blocked(self, login, resident):
        assert login(resident).get(reverse("finance:overview")).status_code == 403


# ------------------------------------------------------------------ reconciliation


class TestReconcile:
    def _deposit(self, bank, cents, on, check=""):
        return Transaction.objects.create(
            bank_account=bank,
            date=on,
            description="DEPOSIT",
            amount_cents=cents,
            source="csv",
            dedupe_key=f"k{cents}{on}{check}",
            check_number=check,
        )

    def test_single_exact_match_confirmed(self, bank, home):
        p = dues.record_payment(home, 30000, "check", date(2027, 1, 3), check_number="501")
        d = self._deposit(bank, 30000, date(2027, 1, 5))
        services.auto_match_deposits()
        m = PaymentMatch.objects.get()
        assert (m.transaction, m.payment, m.status) == (d, p, "confirmed")

    def test_multi_check_deposit_suggested(self, bank, home, other_home):
        dues.record_payment(home, 30000, "check", date(2027, 1, 3), check_number="501")
        dues.record_payment(other_home, 30000, "check", date(2027, 1, 4), check_number="77")
        self._deposit(bank, 60000, date(2027, 1, 6))
        services.auto_match_deposits()
        assert PaymentMatch.objects.filter(status="suggested").count() == 2

    def test_ambiguous_single_is_only_suggested(self, bank, home, other_home):
        dues.record_payment(home, 30000, "check", date(2027, 1, 3), check_number="1")
        dues.record_payment(other_home, 30000, "check", date(2027, 1, 3), check_number="2")
        self._deposit(bank, 30000, date(2027, 1, 5))
        services.auto_match_deposits()
        assert PaymentMatch.objects.get().status == "suggested"

    def test_check_number_breaks_tie(self, bank, home, other_home):
        dues.record_payment(home, 30000, "check", date(2027, 1, 3), check_number="1")
        p2 = dues.record_payment(other_home, 30000, "check", date(2027, 1, 3), check_number="2")
        self._deposit(bank, 30000, date(2027, 1, 5), check="2")
        services.auto_match_deposits()
        m = PaymentMatch.objects.get()
        assert m.payment == p2 and m.status == "confirmed"

    def test_outside_window_or_voided_not_matched(self, bank, home):
        dues.record_payment(home, 30000, "check", date(2026, 11, 1), check_number="1")
        p = dues.record_payment(home, 30000, "check", date(2027, 1, 3), check_number="2")
        dues.void_payment(p, "bounced")
        self._deposit(bank, 30000, date(2027, 1, 5))
        services.auto_match_deposits()
        assert not PaymentMatch.objects.exists()

    def test_manual_match_must_add_up(self, login, board_user, bank, home):
        p = dues.record_payment(home, 25000, "cash", date(2027, 1, 3))
        d = self._deposit(bank, 30000, date(2027, 1, 5))
        with pytest.raises(ValueError):
            services.manual_match(d, [p], board_user)
        login(board_user).post(reverse("finance:reconcile_action", args=[0]), {"action": "auto"})
        assert not PaymentMatch.objects.exists()

    def test_statement_check(self, bank):
        services.import_rows(csv_rows(), bank, "csv")
        StatementBalance.objects.create(
            bank_account=bank, statement_date=date(2027, 1, 31), ending_balance_cents=963077
        )
        StatementBalance.objects.create(
            bank_account=bank, statement_date=date(2027, 2, 28), ending_balance_cents=1000000
        )
        checks = {c.statement.statement_date: c for c in services.statement_checks(bank)}
        assert checks[date(2027, 1, 31)].ok
        assert checks[date(2027, 2, 28)].difference == 1000000 - 1023077


# ------------------------------------------------------------------ tax ledger


class TestLedger:
    def test_summary_nets_refunds_and_skips_transfers(self, bank):
        dues_acct = Account.objects.get(code="4000")
        lawn = Account.objects.get(code="5000")
        transfer = Account.objects.get(code="9000")
        rows = [
            ("2027-01-05", 60000, dues_acct),
            ("2027-01-12", -85000, lawn),
            ("2027-01-20", 5000, lawn),
            ("2027-02-01", -100000, transfer),
            ("2027-02-02", 1234, None),
            ("2026-12-31", 99999, dues_acct),
        ]
        for i, (d, cents, acct) in enumerate(rows):
            Transaction.objects.create(
                bank_account=bank,
                date=d,
                description=f"t{i}",
                amount_cents=cents,
                source="csv",
                dedupe_key=f"t{i}",
                account=acct,
            )
        s = services.ledger_summary(2027)
        assert s["total_income"] == 60000 and s["total_expense"] == 80000 and s["net"] == -20000
        assert s["uncategorized"].count == 1
        assert services.ledger_transactions(2027).count() == 4

    def test_exports(self, login, board_user, bank):
        Transaction.objects.create(
            bank_account=bank,
            date=date(2027, 3, 1),
            description="=CMD()",
            amount_cents=-100,
            source="csv",
            dedupe_key="x",
            account=Account.objects.get(code="5900"),
        )
        client = login(board_user)
        body = client.get(reverse("finance:ledger_csv"), {"year": 2027}).content.decode()
        assert "'=CMD()" in body and "5900" in body
        assert b"Bank fees" in client.get(reverse("finance:ledger_print"), {"year": 2027}).content
        pdf = client.get(reverse("finance:ledger_print"), {"year": 2027, "format": "pdf"})
        assert pdf.content.startswith(b"%PDF")


# ------------------------------------------------------------------ SimpleFIN


@pytest.fixture
def sfin_on(settings):
    from cryptography.fernet import Fernet

    settings.SIMPLEFIN_ENABLED = True
    settings.ENCRYPTION_KEY = Fernet.generate_key().decode()


class TestSimpleFIN:
    ACCOUNTS = {
        "errlist": [],
        "accounts": [
            {
                "id": "ACT-1",
                "name": "Bank Checking",
                "currency": "USD",
                "balance": "9630.77",
                "balance-date": 1,
                "transactions": [
                    {"id": "T1", "posted": 1799020800, "amount": "600.00", "description": "DEPOSIT"},
                    {"id": "T2", "posted": 0, "amount": "-5.00", "description": "PENDING", "pending": True},
                ],
            }
        ],
    }

    def test_disabled_without_key(self, settings):
        settings.SIMPLEFIN_ENABLED = True
        settings.ENCRYPTION_KEY = ""
        assert not simplefin.is_enabled()

    def test_encryption_roundtrip_and_no_plaintext(self, sfin_on):
        secret = "https://user:pass@bridge.example/simplefin"
        token = simplefin.encrypt(secret)
        assert "pass" not in token and simplefin.decrypt(token) == secret

    def test_rejects_non_https_and_private_hosts(self, sfin_on):
        for url in ("http://bridge.example/claim/x", "https://127.0.0.1/claim/x", "https://10.0.0.5/claim/x"):
            with pytest.raises(simplefin.SimpleFINError):
                simplefin.claim(base64.b64encode(url.encode()).decode())

    def test_connect_and_sync(self, sfin_on, bank, board_user):
        calls = []

        def fake_request(url, method="GET", data=None):
            calls.append((method, url))
            if method == "POST":
                return b"https://user:secret@bridge.example/simplefin"
            return json.dumps(self.ACCOUNTS).encode()

        with mock.patch.object(simplefin, "_request", fake_request):
            conn = services.simplefin_connect(
                base64.b64encode(b"https://bridge.example/claim/abc").decode(), board_user
            )
            assert "secret" not in conn.access_url_encrypted
            link = SimpleFINAccountLink.objects.get()
            assert link.remote_name == "Bank Checking" and link.last_balance_cents == 963077
            assert Transaction.objects.count() == 0  # not linked to a bank account yet
            link.bank_account = bank
            link.save()
            services.simplefin_sync()
            services.simplefin_sync()  # overlap is deduped
        assert list(Transaction.objects.values_list("amount_cents", flat=True)) == [60000]  # pending skipped
        assert calls[0] == ("POST", "https://bridge.example/claim/abc")
        assert SimpleFINConnection.objects.get().last_sync_at is not None

    def test_sync_error_recorded(self, sfin_on, board_user):
        conn = SimpleFINConnection.objects.create(
            access_url_encrypted=simplefin.encrypt("https://u:p@x.example/s")
        )
        with mock.patch.object(simplefin, "_request", side_effect=simplefin.SimpleFINError("refused")):
            result = services.simplefin_sync()
        conn.refresh_from_db()
        assert result == {"simplefin_error": "refused"} and conn.last_error == "refused"

    def test_page_off_without_flag(self, login, board_user):
        assert b"is off" in login(board_user).get(reverse("finance:simplefin")).content
