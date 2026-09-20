import uuid
from datetime import UTC, datetime

from api.models import Receipt
from api.services.recurrence import _matches
from finrecur.rules.types import Decision, ReasonCode


def _receipt(**overrides) -> Receipt:
    values = {
        "id": uuid.uuid4(),
        "counterparty_id": uuid.uuid4(),
        "amount": 9_750,
        "received_at": datetime(2026, 1, 1, tzinfo=UTC),
        "reference": "order-1",
        "original_reference": None,
        "memo": None,
        "simulated": True,
        "meta": {"payment_type": "credit_card", "batch": "day2"},
    }
    values.update(overrides)
    return Receipt(**values)


def test_alias_condition_matches_the_incoming_counterparty():
    receipt = _receipt()
    decision = Decision(str(receipt.id), "matched", "R2", "amount_date")
    assert _matches({"counterparty_id": str(receipt.counterparty_id)}, receipt, decision)


def test_fee_condition_uses_payment_type_and_numeric_tolerance():
    receipt = _receipt()
    decision = Decision(
        str(receipt.id),
        "escalated",
        "R1",
        "exact",
        reason_code=ReasonCode.SHORT_PAY,
        evidence={"gap_percent": 2.504},
    )
    assert _matches(
        {"payment_type": "credit_card", "shortfall_pct": 2.5, "tol": 0.01},
        receipt,
        decision,
    )


def test_policy_condition_counts_rows_handled_by_that_rule():
    receipt = _receipt()
    decision = Decision(str(receipt.id), "matched", "R4", "exact")
    assert _matches({"rule_id": "R4", "key": "max_percent", "after": 3.2}, receipt, decision)
    assert not _matches(
        {"rule_id": "R3", "key": "threshold_centavos", "after": 600}, receipt, decision
    )
