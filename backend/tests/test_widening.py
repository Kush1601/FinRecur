from finrecur.fixes.widening import classify


def test_r4_max_percent_up_is_widening():
    assert classify("R4", "max_percent", 2.9, 3.5) == "widening"


def test_r4_max_percent_down_is_narrowing():
    assert classify("R4", "max_percent", 3.5, 3.2) == "narrowing"


def test_r7_tolerance_up_is_widening():
    assert classify("R7", "tolerance_centavos", 1, 3) == "widening"


def test_r2_window_days_up_is_widening():
    assert classify("R2", "window_days", 30, 45) == "widening"


def test_r3_threshold_up_is_widening():
    assert classify("R3", "threshold_centavos", 500, 600) == "widening"


def test_must_fit_shipping_true_to_false_is_widening():
    assert classify("R4", "must_fit_shipping", True, False) == "widening"


def test_must_fit_shipping_false_to_true_is_narrowing():
    assert classify("R4", "must_fit_shipping", False, True) == "narrowing"


def test_cap_removed_is_widening():
    assert classify("R3", "threshold_centavos", 500, None) == "widening"


def test_cap_added_is_narrowing():
    assert classify("R3", "threshold_centavos", None, 500) == "narrowing"


def test_unknown_key_is_unsupported():
    assert classify("R9", "made_up", 1, 2) == "unsupported"


def test_no_change_is_unsupported():
    assert classify("R4", "max_percent", 2.9, 2.9) == "unsupported"
