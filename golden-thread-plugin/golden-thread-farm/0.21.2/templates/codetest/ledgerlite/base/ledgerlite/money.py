"""Money helpers. All amounts are Decimal, quantized to cents with an explicit mode."""
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def to_decimal(value):
    """Convert str, int, float or Decimal to Decimal (floats go through repr)."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def quantize(amount, rounding=ROUND_HALF_UP):
    """Round to whole cents. Half-up is the house rule: 0.005 becomes 0.01."""
    return to_decimal(amount).quantize(CENT, rounding=rounding)


def percent_of(amount, pct):
    """pct percent of amount, rounded half-up to cents."""
    return quantize(to_decimal(amount) * to_decimal(pct) / 100)


def allocate(total, parts):
    """Split total into `parts` cent-exact shares; leftover cents go to the earliest."""
    total = quantize(total)
    if parts < 1:
        raise ValueError("parts must be at least 1")
    if total < 0:
        raise ValueError("cannot allocate a negative total")
    base = (total / parts).quantize(CENT, rounding=ROUND_DOWN)
    shares = [base] * parts
    leftover = int((total - base * parts) / CENT)
    for i in range(leftover):
        shares[i] += CENT
    return shares


def fmt(amount):
    """Fixed two-decimal text, e.g. '12.50'."""
    return f"{quantize(amount):.2f}"
