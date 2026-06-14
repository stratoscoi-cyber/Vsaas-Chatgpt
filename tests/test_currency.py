"""Tests for currency localization (ISO 4217 formatting per locale)."""

from agri_platform.common import currency


def test_usd_en():
    assert currency.format_amount(1234.5, "USD", "en") == "$1,234.50"


def test_ngn_symbol_first():
    assert currency.format_amount(1234.5, "NGN", "en") == "₦1,234.50"


def test_xof_zero_decimals_symbol_after():
    # West African CFA franc has 0 minor units.
    assert currency.format_amount(1000, "XOF", "en") == "1,000 CFA"


def test_tnd_three_decimals():
    assert currency.format_amount(1000, "TND", "en") == "1,000.000 DT"


def test_french_locale_grouping_and_decimal():
    assert currency.format_amount(1234.5, "USD", "fr") == "$1 234,50"


def test_portuguese_locale_grouping_and_decimal():
    assert currency.format_amount(1234.5, "USD", "pt") == "$1.234,50"


def test_unknown_currency_falls_back():
    assert currency.format_amount(100, "XYZ", "en") == "100.00 XYZ"


def test_with_code_suffix():
    assert currency.format_amount(100, "USD", "en", with_code=True) == "$100.00 (USD)"


def test_negative_amount():
    assert currency.format_amount(-50, "USD", "en") == "-$50.00"


def test_all_currencies_includes_african_set():
    codes = {c["code"] for c in currency.all_currencies()}
    assert {"NGN", "KES", "XOF", "ZAR", "EGP", "GHS"} <= codes
