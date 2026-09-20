from finrecur.fixes.bounds import MIN_WIDENING_CLUSTER, check_bounds


def test_d_2_9_to_3_5_is_within_bounds():
    # 0.6 move on 2.9 is 20.7%, under the 25% ceiling; 3.5 is under the 5% percent cap.
    result = check_bounds("max_percent", 2.9, 3.5, is_widening=True, cluster_size=6)
    assert result.within_bounds is True


def test_r7_1_to_3_is_rejected_move_too_large():
    result = check_bounds("tolerance_centavos", 1, 3, is_widening=True, cluster_size=7)
    assert result.within_bounds is False
    assert result.reason is not None and "25%" in result.reason


def test_percent_ceiling_rejects_over_5_percent():
    result = check_bounds("max_percent", 4.9, 5.5, is_widening=True, cluster_size=6)
    assert result.within_bounds is False
    assert result.reason is not None and "ceiling" in result.reason


def test_widening_below_min_cluster_is_rejected():
    result = check_bounds("max_percent", 2.9, 3.1, is_widening=True, cluster_size=2)
    assert result.within_bounds is False
    assert result.reason is not None and str(MIN_WIDENING_CLUSTER) in result.reason


def test_narrowing_ignores_min_cluster_size():
    result = check_bounds("max_percent", 3.5, 3.2, is_widening=False, cluster_size=1)
    assert result.within_bounds is True
