"""Money is stored as integer cents everywhere. These helpers convert at the edges."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import forms


def to_cents(value) -> int:
    """Convert dollars (str/Decimal/int/float) to integer cents, rounding half up."""
    if value is None or value == "":
        return 0
    if isinstance(value, str):
        value = value.replace("$", "").replace(",", "").strip()
    try:
        dollars = Decimal(str(value))
        if not dollars.is_finite() or abs(dollars) >= Decimal("1e12"):
            raise InvalidOperation
        return int((dollars * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        raise ValueError(f"Not a dollar amount: {value!r}") from exc


def to_dollars(cents: int) -> Decimal:
    return (Decimal(int(cents or 0)) / 100).quantize(Decimal("0.01"))


def format_money(cents, signed=False) -> str:
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ("+" if signed and cents > 0 else "")
    return f"{sign}${abs(cents) / 100:,.2f}"


def percent_of(cents: int, percent) -> int:
    """`percent`% of `cents`, rounded half up to the cent."""
    amount = Decimal(int(cents)) * Decimal(str(percent)) / Decimal(100)
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class MoneyField(forms.DecimalField):
    """A dollars input that cleans to integer cents."""

    def __init__(self, *, min_cents=None, **kwargs):
        kwargs.setdefault("max_digits", 12)
        kwargs.setdefault("decimal_places", 2)
        kwargs.setdefault("widget", forms.TextInput(attrs={"inputmode": "decimal"}))
        self.min_cents = min_cents
        super().__init__(**kwargs)

    def to_python(self, value):
        if isinstance(value, str):
            value = value.replace("$", "").replace(",", "").strip()
        return super().to_python(value)

    def prepare_value(self, value):
        if isinstance(value, int) and not isinstance(value, bool):
            return f"{to_dollars(value):.2f}"
        return value

    def clean(self, value):
        dollars = super().clean(value)
        if dollars is None:
            return None
        cents = to_cents(dollars)
        if self.min_cents is not None and cents < self.min_cents:
            raise forms.ValidationError(f"Must be at least {format_money(self.min_cents)}.")
        return cents
