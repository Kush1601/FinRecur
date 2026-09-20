"""Literal-fixture tests for feature extraction (spec 3.4 step 4a)."""

import dataclasses

from finrecur.grouping.features import (
    GroupingItem,
    ReceivableCandidate,
    compute_features,
    subset_sum_indices,
)

_DEFAULT_ITEM = GroupingItem(
    id="item-1",
    kind="exception",
    reason_code="short_pay",
    adjustment_reason=None,
    receipt_id="receipt-1",
    receipt_amount=10_000,
    receipt_date="2017-05-01",
    receipt_reference="order-1",
    payment_type="credit_card",
    counterparty_id="cp-1",
    counterparty_has_own_receivables=True,
    gap_centavos=250,
    matched_receivable=ReceivableCandidate(id="order-1", total=10_250, shipping=500, status="open"),
)


def _item(**overrides) -> GroupingItem:
    return dataclasses.replace(_DEFAULT_ITEM, **overrides)


def test_reason_code_falls_back_to_adjustment_reason_then_rescued():
    exception_item = _item(reason_code="short_pay", adjustment_reason=None)
    assert compute_features(exception_item)["reason_code"] == "short_pay"

    write_off_item = _item(kind="write_off", reason_code=None, adjustment_reason="tolerance")
    assert compute_features(write_off_item)["reason_code"] == "tolerance"

    rescued_item = _item(
        kind="rescued", reason_code=None, adjustment_reason=None, gap_centavos=None
    )
    assert compute_features(rescued_item)["reason_code"] == "rescued"


def test_shortfall_pct_rounds_to_two_decimals():
    item = _item(
        gap_centavos=256,
        matched_receivable=ReceivableCandidate(
            id="order-1", total=10_250, shipping=500, status="open"
        ),
    )
    features = compute_features(item)
    assert features["shortfall_pct"] == round(256 / 10_250 * 100, 2)


def test_shortfall_pct_none_without_a_matched_receivable():
    item = _item(matched_receivable=None)
    assert compute_features(item)["shortfall_pct"] is None


def test_shortfall_eq_shipping_true_when_gap_fits_inside_shipping():
    item = _item(
        gap_centavos=400,
        matched_receivable=ReceivableCandidate(
            id="order-1", total=10_250, shipping=500, status="open"
        ),
    )
    assert compute_features(item)["shortfall_eq_shipping"] is True


def test_shortfall_eq_shipping_false_when_gap_exceeds_shipping():
    item = _item(
        gap_centavos=600,
        matched_receivable=ReceivableCandidate(
            id="order-1", total=10_250, shipping=500, status="open"
        ),
    )
    assert compute_features(item)["shortfall_eq_shipping"] is False


def test_counterparty_norm_lowercases_strips_and_drops_punctuation():
    item = _item()
    features = compute_features(item, counterparty_name="  ACME, Comércio LTDA.  ")
    assert features["counterparty_norm"] == "acme comércio ltda"


def test_counterparty_is_new_when_no_receivables_of_its_own():
    item = _item(counterparty_has_own_receivables=False)
    assert compute_features(item)["counterparty_is_new"] is True

    item2 = _item(counterparty_has_own_receivables=True)
    assert compute_features(item2)["counterparty_is_new"] is False


def test_reference_status_empty():
    item = _item(receipt_reference="")
    assert compute_features(item)["reference_status"] == "empty"


def test_reference_status_exact_match_on_a_nearby_id():
    item = _item(
        receipt_reference="order-1",
        matched_receivable=None,
        nearby_receivable_ids=("order-1", "order-2"),
    )
    assert compute_features(item)["reference_status"] == "exact"


def test_reference_status_edit_distance_one_or_two():
    item = _item(
        receipt_reference="order-x",  # 1 char off from order-1
        matched_receivable=None,
        nearby_receivable_ids=("order-1", "order-2"),
    )
    assert (
        compute_features(item)["reference_status"]
        == "edit_distance_1_or_2_from_some_receivable_source_id"
    )


def test_reference_status_unknown_when_far_from_everything():
    item = _item(
        receipt_reference="completely-different-string",
        matched_receivable=None,
        nearby_receivable_ids=("order-1", "order-2"),
    )
    assert compute_features(item)["reference_status"] == "unknown"


def test_nearest_receivable_source_id_picks_the_closest_candidate():
    item = _item(
        receipt_reference="order-x",
        matched_receivable=None,
        nearby_receivable_ids=("order-1", "totally-different"),
    )
    features = compute_features(item)
    assert features["nearest_receivable_source_id"] == "order-1"
    assert features["nearest_receivable_edit_distance"] == 1


def test_nearest_receivable_source_id_uses_an_exact_reference_without_edit_distance():
    item = _item(
        receipt_reference="order-1",
        matched_receivable=None,
        nearby_receivable_ids=("order-1", "totally-different"),
    )
    features = compute_features(item)
    assert features["nearest_receivable_source_id"] == "order-1"
    assert features["nearest_receivable_edit_distance"] == 0


def test_nearest_receivable_source_id_none_without_nearby_ids_or_reference():
    empty_ref = _item(receipt_reference="", nearby_receivable_ids=("order-1",))
    assert compute_features(empty_ref)["nearest_receivable_source_id"] is None

    no_candidates = _item(receipt_reference="order-x", nearby_receivable_ids=())
    assert compute_features(no_candidates)["nearest_receivable_source_id"] is None


def test_receipt_reference_is_exposed_as_a_feature():
    item = _item(receipt_reference="order-corrupted")
    assert compute_features(item)["receipt_reference"] == "order-corrupted"


def test_duplicate_fingerprint_matches_for_identical_amount_date_reference():
    a = _item(id="a", receipt_amount=500, receipt_date="2017-01-01", receipt_reference="ref-1")
    b = _item(id="b", receipt_amount=500, receipt_date="2017-01-01", receipt_reference="ref-1")
    c = _item(id="c", receipt_amount=501, receipt_date="2017-01-01", receipt_reference="ref-1")
    fa, fb, fc = (compute_features(x)["duplicate_fingerprint"] for x in (a, b, c))
    assert fa == fb
    assert fa != fc


def test_seller_id_only_set_when_single_seller():
    single = _item(
        matched_receivable=ReceivableCandidate(
            id="order-1", total=1000, shipping=100, status="open", seller_ids=("seller-1",)
        )
    )
    assert compute_features(single)["seller_id"] == "seller-1"

    multi = _item(
        matched_receivable=ReceivableCandidate(
            id="order-1",
            total=1000,
            shipping=100,
            status="open",
            seller_ids=("seller-1", "seller-2"),
        )
    )
    assert compute_features(multi)["seller_id"] is None


def test_overpayment_matches_multi_receivable_via_subset_sum():
    item = _item(
        reason_code="overpayment",
        matched_receivable=ReceivableCandidate(
            id="order-1", total=4_000, shipping=0, status="open"
        ),
        counterparty_open_receivable_totals=(3_000,),
        receipt_amount=7_000,
    )
    assert compute_features(item)["overpayment_matches_multi_receivable"] is True


def test_subset_sum_indices_supports_a_three_receivable_split():
    assert subset_sum_indices([1_000, 2_000, 4_000], 7_000) == (0, 1, 2)


def test_overpayment_matches_multi_receivable_false_when_no_subset_sums():
    item = _item(
        reason_code="overpayment",
        matched_receivable=ReceivableCandidate(
            id="order-1", total=4_000, shipping=0, status="open"
        ),
        counterparty_open_receivable_totals=(3_000,),
        receipt_amount=9_999,
    )
    assert compute_features(item)["overpayment_matches_multi_receivable"] is False


def test_overpayment_feature_false_for_other_reason_codes():
    item = _item(reason_code="short_pay")
    assert compute_features(item)["overpayment_matches_multi_receivable"] is False


def test_overpayment_feature_handles_many_open_receivables_without_subset_enumeration():
    item = GroupingItem(
        id="exception-1",
        kind="exception",
        reason_code="overpayment",
        adjustment_reason=None,
        receipt_id="receipt-1",
        receipt_amount=2_001,
        receipt_date="2026-01-01",
        receipt_reference="",
        payment_type="boleto",
        counterparty_id="counterparty-1",
        counterparty_has_own_receivables=True,
        gap_centavos=1,
        counterparty_open_receivable_totals=tuple(range(10_000, 10_060)),
        matched_receivable=ReceivableCandidate(
            id="receivable-1", total=2_000, shipping=0, status="open"
        ),
    )

    assert compute_features(item)["overpayment_matches_multi_receivable"] is False
