"""D002 uses continuous decimal seconds and ceilings absolute event timestamps."""

from decimal import Decimal, InvalidOperation, ROUND_CEILING
from fractions import Fraction


def nonnegative_decimal(value, unit):
    """Keep decimal input and the round-trip spelling of a P0 float weight."""
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(f"Invalid {unit}: {value!r}") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{unit} must be finite and nonnegative: {value!r}")
    return number


def seconds(value) -> Decimal:
    return nonnegative_decimal(value, "seconds")


def exact_add(first_s, second_s) -> Decimal:
    """Add durations/absolute times with integer arithmetic at their common scale."""
    first, second = seconds(first_s), seconds(second_s)
    places = max(0, -first.as_tuple().exponent, -second.as_tuple().exponent)
    scale = 10 ** places
    total = 0
    for term in (first, second):
        numerator, denominator = term.as_integer_ratio()
        total += numerator * scale // denominator
    return Decimal((0, Decimal(total).as_tuple().digits, -places))


def require_tick(value) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"Expected a nonnegative integer simulation second: {value!r}")
    return value


def event_second(value) -> int:
    """Apply once to the absolute physical instant (including subsecond carry)."""
    return int(seconds(value).to_integral_value(rounding=ROUND_CEILING))


def objective_minutes(duration_s) -> float:
    """D005 / Eq. (2): convert at the scoring boundary without rounding to minutes."""
    return float(Fraction(seconds(duration_s)) / 60)
