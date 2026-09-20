"""Literal-fixture tests for each grouping strategy (spec 3.4 step 4a), one per
fault shape from spec 3.10, plus min-size and ordering behaviour."""

import dataclasses

from finrecur.grouping.bucket import GroupingConfig, group
from finrecur.grouping.features import GroupingItem, ReceivableCandidate


def _receivable(
    order_id: str, total: int, shipping: int, seller_id: str = "seller-1"
) -> ReceivableCandidate:
    return ReceivableCandidate(
        id=order_id, total=total, shipping=shipping, status="open", seller_ids=(seller_id,)
    )


def _exception_item(item_id: str, **overrides) -> GroupingItem:
    default = GroupingItem(
        id=item_id,
        kind="exception",
        reason_code="short_pay",
        adjustment_reason=None,
        receipt_id=f"receipt-{item_id}",
        receipt_amount=10_000,
        receipt_date="2017-05-01",
        receipt_reference=f"order-{item_id}",
        payment_type="credit_card",
        counterparty_id=f"cp-{item_id}",
        counterparty_has_own_receivables=True,
        gap_centavos=None,
        matched_receivable=None,
    )
    return dataclasses.replace(default, **overrides)


def test_fault_e_shaped_mixed_group_of_escalated_and_absorbed():
    """4 escalated short_pay + 7 absorbed tolerance write-offs, all ~2.50% of total,
    same payment_type -- must land in one `mixed` group of 11."""
    items = []
    for i in range(4):
        total = 10_000 + i
        gap = round(total * 0.025)
        items.append(
            _exception_item(
                f"esc-{i}",
                kind="exception",
                reason_code="short_pay",
                gap_centavos=gap,
                matched_receivable=_receivable(f"order-esc-{i}", total, 0),
            )
        )
    for i in range(7):
        total = 20_000 + i
        gap = round(total * 0.025)
        items.append(
            _exception_item(
                f"abs-{i}",
                kind="write_off",
                reason_code=None,
                adjustment_reason="tolerance",
                gap_centavos=gap,
                matched_receivable=_receivable(f"order-abs-{i}", total, 0),
            )
        )

    result = group(items)
    assert len(result.groups) == 1
    g = result.groups[0]
    assert g.kind == "mixed"
    assert set(g.member_ids) == {i.id for i in items}
    assert not result.singletons


def test_fault_b_shaped_rescued_group():
    items = [
        _exception_item(
            f"r{i}",
            kind="rescued",
            reason_code=None,
            adjustment_reason=None,
            gap_centavos=None,
            receipt_reference=f"order-{i}x",  # 1-char off from "order-{i}"
            matched_receivable=None,
            nearby_receivable_ids=(f"order-{i}",),
        )
        for i in range(5)
    ]
    result = group(items)
    assert len(result.groups) == 1
    assert result.groups[0].kind == "rescued"
    assert len(result.groups[0].member_ids) == 5


def test_fault_a_shaped_new_counterparty_group():
    items = [
        _exception_item(
            f"a{i}",
            reason_code="no_match",
            counterparty_has_own_receivables=False,
        )
        for i in range(5)
    ]
    result = group(items)
    assert len(result.groups) == 1
    assert result.groups[0].kind == "escalated"
    assert len(result.groups[0].member_ids) == 5


def test_fault_d_shaped_seller_shortfall_excludes_other_sellers():
    d_items = [
        _exception_item(
            f"d{i}",
            reason_code="short_pay",
            gap_centavos=300,
            matched_receivable=_receivable(f"order-d{i}", 10_000, 500, seller_id="seller-D"),
        )
        for i in range(6)
    ]
    d2_items = [
        _exception_item(
            f"d2-{i}",
            reason_code="short_pay",
            gap_centavos=350,
            matched_receivable=_receivable(f"order-d2-{i}", 10_000, 500, seller_id="seller-D2"),
        )
        for i in range(2)
    ]
    result = group(d_items + d2_items)
    assert len(result.groups) == 1
    g = result.groups[0]
    assert g.kind == "escalated"
    assert set(g.member_ids) == {i.id for i in d_items}
    assert set(result.singletons) == {i.id for i in d2_items}


def test_fault_c_shaped_duplicate_fingerprint_family_below_min_size_stays_singleton():
    """A single original+duplicate pair shares a fingerprint but only has 2 members,
    below GroupingConfig.min_group_size -- it must not force-group."""
    original = _exception_item(
        "orig",
        reason_code="suspected_duplicate",
        receipt_amount=500,
        receipt_date="2017-01-01",
        receipt_reference="order-1",
    )
    dupe = _exception_item(
        "dupe",
        reason_code="suspected_duplicate",
        receipt_amount=500,
        receipt_date="2017-01-01",
        receipt_reference="order-1",
    )
    result = group([original, dupe])
    assert result.groups == ()
    assert set(result.singletons) == {"orig", "dupe"}


def test_duplicate_fingerprint_family_of_three_or_more_groups():
    items = [
        _exception_item(
            f"dup{i}",
            reason_code="suspected_duplicate",
            receipt_amount=500,
            receipt_date="2017-01-01",
            receipt_reference="order-1",
        )
        for i in range(3)
    ]
    result = group(items)
    assert len(result.groups) == 1
    assert result.groups[0].kind == "escalated"


def test_fault_f_shaped_overpayment_split_group():
    items = [
        _exception_item(
            f"f{i}",
            reason_code="overpayment",
            matched_receivable=_receivable(f"order-f{i}", 4_000, 0),
            counterparty_open_receivable_totals=(3_000,),
            receipt_amount=7_000,
        )
        for i in range(3)
    ]
    result = group(items)
    assert len(result.groups) == 1
    assert result.groups[0].kind == "escalated"
    assert len(result.groups[0].member_ids) == 3


def test_payment_on_cancelled_real_rows_do_not_group_on_reason_code_alone():
    """28 unrelated cancelled-order rows share only reason_code -- no other feature
    aligns them, so they stay singletons rather than one spurious cluster."""
    items = [
        _exception_item(
            f"c{i}",
            reason_code="payment_on_cancelled",
            counterparty_has_own_receivables=True,
            counterparty_id=f"cp-{i}",
        )
        for i in range(28)
    ]
    result = group(items)
    assert result.groups == ()
    assert len(result.singletons) == 28


def test_min_group_size_enforced_across_strategies():
    items = [
        _exception_item(f"a{i}", reason_code="no_match", counterparty_has_own_receivables=False)
        for i in range(2)
    ]
    result = group(items, config=GroupingConfig(min_group_size=3))
    assert result.groups == ()
    assert len(result.singletons) == 2


def test_first_strategy_wins_item_is_not_double_claimed():
    """An item eligible for the shortfall-ratio strategy is claimed there and never
    reconsidered by a later strategy, even if it would also qualify."""
    items = [
        _exception_item(
            f"e{i}",
            reason_code="short_pay",
            gap_centavos=250,
            payment_type="credit_card",
            counterparty_has_own_receivables=False,  # would also fit new_counterparty
            matched_receivable=_receivable(f"order-e{i}", 10_000, 0),
        )
        for i in range(3)
    ]
    result = group(items)
    assert sum(len(g.member_ids) for g in result.groups) == 3
    assert len({m for g in result.groups for m in g.member_ids}) == 3
