"""Validate + apply_to_state + inverse round-trips, on literal fixtures. Spec 3.4
step 9: "inverse round-trips for F1-F5."""

from datetime import UTC, datetime

import pytest

from finrecur.fixes.engine import (
    Rows,
    State,
    StateAdjustment,
    StateAllocation,
    StateReceipt,
    StateReceivable,
    apply_to_state,
    validate,
)
from finrecur.result import Err, Ok

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _receivable(id_, total, shipping=0, status="open") -> StateReceivable:
    return StateReceivable(
        id=id_,
        counterparty_id="cp-canon",
        total=total,
        shipping=shipping,
        allocated=0,
        adjusted=0,
        remaining=total,
        status=status,
        issued_at=NOW,
    )


def _receipt(id_, amount, counterparty_id="cp-variant", reference="") -> StateReceipt:
    return StateReceipt(
        id=id_, counterparty_id=counterparty_id, amount=amount, received_at=NOW, reference=reference
    )


def _rows(state: State) -> Rows:
    return Rows(
        receipt_ids=frozenset(state.receipts),
        receivable_ids=frozenset(state.receivables),
        receipt_amounts={r.id: r.amount for r in state.receipts.values()},
        policy=state.policy,
    )


@pytest.fixture
def base_state() -> State:
    return State(
        receivables={"rv-1": _receivable("rv-1", 10000), "rv-2": _receivable("rv-2", 5000)},
        receipts={"r-1": _receipt("r-1", 10000), "r-2": _receipt("r-2", 5000)},
        allocations=[],
        adjustments=[],
        aliases={},
        policy={
            "R4": {"max_percent": 2.9, "must_fit_shipping": True},
            "R7": {"tolerance_centavos": 1},
        },
    )


def test_f1_add_counterparty_alias_round_trip(base_state):
    params = {"alias_counterparty_id": "cp-variant", "canonical_counterparty_id": "cp-canon"}
    assert validate("add_counterparty_alias", params, _rows(base_state)) == Ok(None)

    new_state, inverse = apply_to_state("add_counterparty_alias", params, base_state)
    assert new_state.aliases["cp-variant"] == "cp-canon"
    assert base_state.aliases == {}  # original untouched

    assert inverse == {"alias_counterparty_id": "cp-variant"}
    del new_state.aliases[inverse["alias_counterparty_id"]]
    assert new_state.aliases == {}


def test_f2_repair_reference_round_trip(base_state):
    base_state.receipts["r-1"].reference = "0RD3R1"
    params = {"repairs": [{"receipt_id": "r-1", "from_ref": "0RD3R1", "to_ref": "ORDER1"}]}
    assert validate("repair_reference", params, _rows(base_state)) == Ok(None)

    new_state, inverse = apply_to_state("repair_reference", params, base_state)
    assert new_state.receipts["r-1"].reference == "ORDER1"
    assert new_state.receipts["r-1"].original_reference == "0RD3R1"

    reverted, _ = apply_to_state(
        "repair_reference",
        {"repairs": [{"receipt_id": "r-1", "to_ref": inverse["repairs"][0]["to_ref"]}]},
        new_state,
    )
    assert reverted.receipts["r-1"].reference == "0RD3R1"


def test_f3_mark_duplicate_round_trip(base_state):
    params = {"pairs": [{"receipt_id": "r-2", "duplicate_of": "r-1"}]}
    assert validate("mark_duplicate", params, _rows(base_state)) == Ok(None)

    new_state, inverse = apply_to_state("mark_duplicate", params, base_state)
    assert new_state.receipts["r-2"].duplicate_of == "r-1"
    assert inverse == {"pairs": [{"receipt_id": "r-2"}]}

    new_state.receipts["r-2"].duplicate_of = None
    assert new_state.receipts["r-2"].duplicate_of is None


def test_f3_rejects_self_duplicate(base_state):
    result = validate(
        "mark_duplicate",
        {"pairs": [{"receipt_id": "r-1", "duplicate_of": "r-1"}]},
        _rows(base_state),
    )
    assert isinstance(result, Err)


def test_f4_split_receipt_round_trip(base_state):
    rv3 = _receivable("rv-3", 3000)
    base_state.receivables["rv-3"] = rv3
    receipt = _receipt("r-3", 8000)
    base_state.receipts["r-3"] = receipt
    params = {
        "splits": [
            {
                "receipt_id": "r-3",
                "allocations": [
                    {"receivable_id": "rv-1", "amount": 5000},
                    {"receivable_id": "rv-3", "amount": 3000},
                ],
            }
        ]
    }
    assert validate("split_receipt", params, _rows(base_state)) == Ok(None)

    new_state, inverse = apply_to_state("split_receipt", params, base_state)
    assert new_state.receivables["rv-1"].allocated == 5000
    assert new_state.receivables["rv-3"].remaining == 0
    assert len(new_state.allocations) == 2
    assert inverse == {"splits": [{"receipt_id": "r-3"}]}


def test_f4_rejects_allocations_not_summing_to_receipt(base_state):
    params = {
        "splits": [
            {"receipt_id": "r-1", "allocations": [{"receivable_id": "rv-2", "amount": 4999}]}
        ]
    }
    result = validate("split_receipt", params, _rows(base_state))
    assert isinstance(result, Err)


def test_f5_record_fee_deduction_absorbed_member_round_trip(base_state):
    # Was silently absorbed: fully allocated, plus a tolerance write-off for the
    # "shortfall" that was actually a processor fee.
    base_state.allocations.append(
        StateAllocation(receipt_id="r-2", receivable_id="rv-2", amount=4875)
    )
    base_state.adjustments.append(
        StateAdjustment(receivable_id="rv-2", receipt_id="r-2", amount=125, reason="tolerance")
    )
    base_state.receivables["rv-2"].allocated = 4875
    base_state.receivables["rv-2"].adjusted = 125
    base_state.receivables["rv-2"].remaining = 0

    params = {"receipt_ids": ["r-2"], "fee_percent": 2.5, "payment_type": "credit_card"}
    assert validate("record_fee_deduction", params, _rows(base_state)) == Ok(None)

    new_state, inverse = apply_to_state("record_fee_deduction", params, base_state)
    fee_adjustments = [a for a in new_state.adjustments if a.reason == "fee"]
    assert len(fee_adjustments) == 1
    assert fee_adjustments[0].amount == 125
    assert not any(a.reason == "tolerance" for a in new_state.adjustments)
    assert new_state.receivables["rv-2"].remaining == 0  # net balance unchanged
    assert inverse["removed_adjustments"] == [
        {"receivable_id": "rv-2", "receipt_id": "r-2", "amount": 125, "reason": "tolerance"}
    ]
    assert inverse["fee_amounts"] == {"r-2": 125}


def test_f5_record_fee_deduction_escalated_member_allocates(base_state):
    # Was escalated: never allocated at all.
    params = {"receipt_ids": ["r-1"], "fee_percent": 2.5, "payment_type": "credit_card"}
    hint = {"r-1": "rv-1"}
    assert validate("record_fee_deduction", params, _rows(base_state)) == Ok(None)

    new_state, inverse = apply_to_state("record_fee_deduction", params, base_state, hint)
    assert len(new_state.allocations) == 1
    assert new_state.allocations[0].amount == 10000
    fee_adjustments = [a for a in new_state.adjustments if a.reason == "fee"]
    assert len(fee_adjustments) == 1
    assert new_state.receivables["rv-1"].remaining == 0
    assert inverse["new_allocation_receipt_ids"] == ["r-1"]


def test_f6_amend_policy_round_trip(base_state):
    params = {
        "rule_id": "R4",
        "key": "max_percent",
        "before": 2.9,
        "after": 3.5,
        "justification": "widen for D",
    }
    assert validate("amend_policy", params, _rows(base_state)) == Ok(None)

    new_state, inverse = apply_to_state("amend_policy", params, base_state)
    assert new_state.policy["R4"]["max_percent"] == 3.5
    assert inverse == {
        "rule_id": "R4",
        "key": "max_percent",
        "before": 3.5,
        "after": 2.9,
        "justification": "revert",
    }

    reverted, _ = apply_to_state("amend_policy", inverse, new_state)
    assert reverted.policy["R4"]["max_percent"] == 2.9


def test_amend_policy_rejects_stale_before(base_state):
    params = {"rule_id": "R4", "key": "max_percent", "before": 99, "after": 3.5}
    result = validate("amend_policy", params, _rows(base_state))
    assert isinstance(result, Err)


def test_manual_review_requires_reason(base_state):
    assert isinstance(validate("manual_review", {}, _rows(base_state)), Err)
    assert validate("manual_review", {"reason": "needs a human"}, _rows(base_state)) == Ok(None)
