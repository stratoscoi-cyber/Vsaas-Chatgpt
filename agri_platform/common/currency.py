"""Currency localization: formatting amounts per ISO 4217 + locale.

The currency table is standard ISO 4217 reference data (code, symbol, number of
minor units / decimals, symbol placement) — stable, factual reference data, not
business assumptions. Locale only affects digit grouping and the decimal mark
(e.g. French uses a narrow space + comma). Unknown currencies fall back to a safe
generic format (code as the symbol, two decimals) rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict


@dataclass(frozen=True)
class Currency:
    code: str
    name: str
    symbol: str
    decimals: int
    symbol_first: bool
    space: bool  # space between symbol and number

    def to_dict(self) -> Dict:
        return {
            "code": self.code,
            "name": self.name,
            "symbol": self.symbol,
            "decimals": self.decimals,
            "symbol_first": self.symbol_first,
        }


# ISO 4217 reference data for the currencies used by the supported regions.
CURRENCIES: Dict[str, Currency] = {
    "USD": Currency("USD", "US Dollar", "$", 2, True, False),
    "NGN": Currency("NGN", "Nigerian Naira", "₦", 2, True, False),
    "GHS": Currency("GHS", "Ghanaian Cedi", "₵", 2, True, False),
    "XOF": Currency("XOF", "West African CFA Franc", "CFA", 0, False, True),
    "XAF": Currency("XAF", "Central African CFA Franc", "FCFA", 0, False, True),
    "KES": Currency("KES", "Kenyan Shilling", "KSh", 2, True, True),
    "TZS": Currency("TZS", "Tanzanian Shilling", "TSh", 2, True, True),
    "UGX": Currency("UGX", "Ugandan Shilling", "USh", 0, True, True),
    "RWF": Currency("RWF", "Rwandan Franc", "FRw", 0, True, True),
    "ETB": Currency("ETB", "Ethiopian Birr", "Br", 2, True, True),
    "ZAR": Currency("ZAR", "South African Rand", "R", 2, True, False),
    "ZMW": Currency("ZMW", "Zambian Kwacha", "ZK", 2, True, True),
    "AOA": Currency("AOA", "Angolan Kwanza", "Kz", 2, True, True),
    "MZN": Currency("MZN", "Mozambican Metical", "MT", 2, False, True),
    "EGP": Currency("EGP", "Egyptian Pound", "E£", 2, True, True),
    "MAD": Currency("MAD", "Moroccan Dirham", "DH", 2, True, True),
    "TND": Currency("TND", "Tunisian Dinar", "DT", 3, False, True),
    "CDF": Currency("CDF", "Congolese Franc", "FC", 2, False, True),
}

# Locale -> (grouping separator, decimal mark).
_SEPARATORS = {
    "en": (",", "."),
    "sw": (",", "."),
    "ha": (",", "."),
    "ar": (",", "."),
    "fr": (" ", ","),   # narrow no-break space
    "pt": (".", ","),
}


def get_currency(code: str) -> Currency:
    return CURRENCIES.get((code or "").upper(), Currency(code or "?", code or "Unknown", code or "?", 2, False, True))


def _group(integer_digits: str, sep: str) -> str:
    out = []
    for i, ch in enumerate(reversed(integer_digits)):
        if i and i % 3 == 0:
            out.append(sep)
        out.append(ch)
    return "".join(reversed(out))


def format_amount(amount, code: str, locale: str = "en", with_code: bool = False) -> str:
    """Format ``amount`` in ``code`` for ``locale`` (e.g. ``₦1,234.50``)."""
    cur = get_currency(code)
    group_sep, dec_sep = _SEPARATORS.get((locale or "en").split("-")[0].lower(), _SEPARATORS["en"])

    q = Decimal(1).scaleb(-cur.decimals) if cur.decimals else Decimal(1)
    value = Decimal(str(amount)).quantize(q, rounding=ROUND_HALF_UP)
    sign = "-" if value < 0 else ""
    value = abs(value)

    digits = f"{value:.{cur.decimals}f}"
    if cur.decimals:
        int_part, frac_part = digits.split(".")
        number = _group(int_part, group_sep) + dec_sep + frac_part
    else:
        number = _group(digits, group_sep)

    gap = " " if cur.space else ""
    body = f"{cur.symbol}{gap}{number}" if cur.symbol_first else f"{number}{gap}{cur.symbol}"
    result = f"{sign}{body}"
    return f"{result} ({cur.code})" if with_code else result


def all_currencies() -> list:
    return [c.to_dict() for c in CURRENCIES.values()]
