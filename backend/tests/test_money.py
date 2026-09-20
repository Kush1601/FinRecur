import pytest

from finrecur.money import format_brl, from_float, parse_brl, pct_of


class TestParseBrl:
    def test_plain_decimal(self):
        assert parse_brl("123.45") == 12345

    def test_brl_with_symbol(self):
        assert parse_brl("R$ 123,45") == 12345

    def test_brl_with_thousands(self):
        assert parse_brl("1.234,56") == 123456

    def test_bare_integer(self):
        assert parse_brl("123") == 12300

    def test_negative(self):
        assert parse_brl("-R$ 12,34") == -1234

    def test_zero(self):
        assert parse_brl("0") == 0
        assert parse_brl("0,00") == 0

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            parse_brl("not money")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            parse_brl("")

    def test_rejects_double_comma(self):
        with pytest.raises(ValueError):
            parse_brl("12,34,56")


class TestFormatBrl:
    def test_basic(self):
        assert format_brl(123456) == "R$ 1.234,56"

    def test_small(self):
        assert format_brl(12345) == "R$ 123,45"

    def test_zero(self):
        assert format_brl(0) == "R$ 0,00"

    def test_negative(self):
        assert format_brl(-1234) == "-R$ 12,34"


class TestPctOf:
    def test_basic(self):
        assert pct_of(29, 1000) == 2.9

    def test_zero_whole(self):
        assert pct_of(10, 0) == 0.0

    def test_zero_part(self):
        assert pct_of(0, 1000) == 0.0

    def test_precision(self):
        assert pct_of(1, 3) == 33.333


class TestFromFloat:
    def test_basic(self):
        assert from_float(1.23) == 123

    def test_zero(self):
        assert from_float(0.0) == 0

    def test_negative(self):
        assert from_float(-1.23) == -123

    def test_half_up_rounding(self):
        assert from_float(0.005) == 1
        assert from_float(1.005) == 101
