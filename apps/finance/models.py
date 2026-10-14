from django.conf import settings
from django.db import models
from django.db.models import Q


class Account(models.Model):
    """Chart of accounts entry used to categorize money in and out."""

    class Type(models.TextChoices):
        INCOME = "income", "Income"
        EXPENSE = "expense", "Expense"
        TRANSFER = "transfer", "Transfer (not income or expense)"

    class TaxClass(models.TextChoices):
        EXEMPT_INCOME = "exempt_income", "Exempt-function income (dues, assessments)"
        NONEXEMPT_INCOME = "nonexempt_income", "Non-exempt income (interest, outside rentals...)"
        NONEXEMPT_EXPENSE = "nonexempt_expense", "Expense directly tied to non-exempt income"
        OTHER = "other", "Other / not applicable"

    code = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=100)
    type = models.CharField(max_length=10, choices=Type.choices)
    tax_class = models.CharField(
        max_length=20,
        choices=TaxClass.choices,
        default=TaxClass.OTHER,
        help_text="Used by the Form 1120-H worksheet. Ask your accountant if unsure.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} {self.name}"


class BankAccount(models.Model):
    name = models.CharField(max_length=100, help_text="e.g. 'Operating checking'")
    last4 = models.CharField("last 4 digits", max_length=4, blank=True)
    is_reserve = models.BooleanField(default=False, help_text="Reserve / savings account.")
    opening_balance_cents = models.BigIntegerField(default=0)
    opening_date = models.DateField(help_text="Balance as of the start of this day.")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name}{f' (…{self.last4})' if self.last4 else ''}"


class ImportMapping(models.Model):
    """Saved column mapping for a CSV layout, reused on later imports."""

    name = models.CharField(max_length=100, unique=True)
    columns = models.JSONField(default=dict)  # {"date": "Posting Date", "amount": "Amount", ...}
    date_format = models.CharField(max_length=20, blank=True)
    category_map = models.JSONField(default=dict, blank=True)  # old category name -> account id

    def __str__(self):
        return self.name


class ImportBatch(models.Model):
    class Source(models.TextChoices):
        CSV = "csv", "CSV file"
        OFX = "ofx", "OFX/QFX file"
        SIMPLEFIN = "simplefin", "SimpleFIN sync"
        HISTORICAL = "historical", "Historical spreadsheet"

    bank_account = models.ForeignKey(
        BankAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="batches"
    )
    source = models.CharField(max_length=12, choices=Source.choices)
    filename = models.CharField(max_length=200, blank=True)
    rows_total = models.PositiveIntegerField(default=0)
    rows_imported = models.PositiveIntegerField(default=0)
    rows_duplicate = models.PositiveIntegerField(default=0)
    rows_error = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_source_display()} {self.filename} ({self.created_at:%Y-%m-%d})"


class CategoryRule(models.Model):
    class Match(models.TextChoices):
        CONTAINS = "contains", "contains"
        STARTS = "starts", "starts with"
        EQUALS = "equals", "is exactly"

    class Direction(models.TextChoices):
        ANY = "any", "Money in or out"
        IN = "in", "Money in"
        OUT = "out", "Money out"

    priority = models.PositiveSmallIntegerField(default=100, help_text="Lower numbers are checked first.")
    match = models.CharField(max_length=10, choices=Match.choices, default=Match.CONTAINS)
    pattern = models.CharField(
        max_length=100, help_text="Text to look for in the description (not case sensitive)."
    )
    direction = models.CharField(max_length=5, choices=Direction.choices, default=Direction.ANY)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="rules")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["priority", "id"]

    def __str__(self):
        return f"{self.get_match_display()} '{self.pattern}' → {self.account}"

    def matches(self, description, amount_cents):
        if self.direction == self.Direction.IN and amount_cents <= 0:
            return False
        if self.direction == self.Direction.OUT and amount_cents >= 0:
            return False
        text, pattern = (description or "").casefold(), self.pattern.casefold().strip()
        if not pattern:
            return False
        if self.match == self.Match.STARTS:
            return text.startswith(pattern)
        if self.match == self.Match.EQUALS:
            return text.strip() == pattern
        return pattern in text


class Transaction(models.Model):
    class Categorized(models.TextChoices):
        NONE = "", "Not categorized"
        RULE = "rule", "By rule"
        MANUAL = "manual", "By hand"
        IMPORT = "import", "From import"

    bank_account = models.ForeignKey(
        BankAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="transactions"
    )
    date = models.DateField(db_index=True)
    description = models.CharField(max_length=300)
    amount_cents = models.BigIntegerField(help_text="Positive = money in, negative = money out.")
    source = models.CharField(max_length=12, choices=ImportBatch.Source.choices)
    dedupe_key = models.CharField(max_length=80)
    fitid = models.CharField(max_length=100, blank=True)
    check_number = models.CharField(max_length=20, blank=True)
    memo = models.CharField(max_length=300, blank=True)
    account = models.ForeignKey(
        Account, null=True, blank=True, on_delete=models.SET_NULL, related_name="transactions"
    )
    categorized_by = models.CharField(max_length=8, choices=Categorized.choices, blank=True, default="")
    rule = models.ForeignKey(CategoryRule, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    batch = models.ForeignKey(
        ImportBatch, null=True, blank=True, on_delete=models.SET_NULL, related_name="transactions"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["bank_account", "dedupe_key"],
                condition=Q(bank_account__isnull=False),
                name="unique_bank_transaction",
            )
        ]

    def __str__(self):
        return f"{self.date} {self.description} {self.amount_cents}"


class PaymentMatch(models.Model):
    """Links a bank deposit to the dues payment(s) it contains (a deposit often has several checks)."""

    class Status(models.TextChoices):
        SUGGESTED = "suggested", "Suggested"
        CONFIRMED = "confirmed", "Confirmed"

    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="payment_matches")
    payment = models.OneToOneField("dues.Payment", on_delete=models.CASCADE, related_name="bank_match")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SUGGESTED)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)


class StatementBalance(models.Model):
    bank_account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name="statements")
    statement_date = models.DateField(help_text="Closing date on the statement.")
    ending_balance_cents = models.BigIntegerField()
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-statement_date"]
        constraints = [
            models.UniqueConstraint(fields=["bank_account", "statement_date"], name="one_statement_per_date")
        ]


class SimpleFINConnection(models.Model):
    """Optional read-only bank sync. The access URL contains credentials, so it's encrypted."""

    access_url_encrypted = models.TextField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    def __str__(self):
        return "SimpleFIN connection"


class SimpleFINAccountLink(models.Model):
    connection = models.ForeignKey(SimpleFINConnection, on_delete=models.CASCADE, related_name="links")
    remote_id = models.CharField(max_length=200)
    remote_name = models.CharField(max_length=200)
    bank_account = models.ForeignKey(
        BankAccount, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    last_balance_cents = models.BigIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["connection", "remote_id"], name="one_link_per_remote")
        ]
