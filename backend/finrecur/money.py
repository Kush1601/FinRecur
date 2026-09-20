"""Integer-centavos money helpers. No floats in amounts, ever."""

import re
from decimal import ROUND_HALF_UP, Decimal

Centavos = int

# "123.45", "R$ 123,45", "1.234,56", "123" — anything else is rejected.
_PLAIN = re.compile(r"^-?\d+(\.\d{1,2})?$")
_BRL = re.compile(r"^R?\$?\s*-?(\d{1,3}(\.\d{3})*|\d+),\d{2}$")
_BRL_NO_THOUSANDS = re.compile(r"^R?\$?\s*-?\d+,\d{2}$")


def parse_brl(s: str) -> int:
    """Parse a BRL amount string into centavos. Accepts plain decimal ("123.45"),
    BRL-formatted ("R$ 123,45", "1.234,56") and bare integers ("123")."""
    raw = s.strip()
    if not raw:
        raise ValueError(f"empty amount: {s!r}")

    negative = raw.startswith("-")
    body = raw[1:] if negative else raw
    body = body.replace("R$", "").replace("$", "").strip()

    if "," in body:
        # BRL style: "." is a thousands separator, "," is the decimal point.
        if not re.fullmatch(r"\d{1,3}(\.\d{3})*,\d{2}|\d+,\d{2}", body):
            raise ValueError(f"not a valid BRL amount: {s!r}")
        body = body.replace(".", "").replace(",", ".")
    else:
        if not re.fullmatch(r"\d+(\.\d{1,2})?", body):
            raise ValueError(f"not a valid amount: {s!r}")

    value = Decimal(body)
    cents = int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))
    return -cents if negative else cents


def format_brl(c: int) -> str:
    """Format centavos as a BRL string, e.g. 123456 -> "R$ 1.234,56"."""
    negative = c < 0
    c = abs(c)
    reais, cents = divmod(c, 100)
    grouped = f"{reais:,}".replace(",", ".")
    sign = "-" if negative else ""
    return f"{sign}R$ {grouped},{cents:02d}"


def pct_of(part: int, whole: int) -> float:
    """Percent that `part` is of `whole`, to 3 decimals. 0 whole -> 0.0."""
    if whole == 0:
        return 0.0
    return round((part / whole) * 100, 3)


def from_float(x: float) -> int:
    """Round a float amount to centavos, half up."""
    return int(Decimal(str(x)).scaleb(2).to_integral_value(rounding=ROUND_HALF_UP))
