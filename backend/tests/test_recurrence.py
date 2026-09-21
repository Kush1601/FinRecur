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


def test_policy_condition_matches_the_receipts_own_gap_not_the_final_rule():
    # A shortfall in the band an amend_policy fix widened R4 to cover -- whether
    # the CURRENT policy settles it (rule_id R4, matched) or a later, tighter
    # policy escalates the same receipt under a different rule (R8), the
    # condition itself "appeared" either way. Keying off decision.rule_id was
    # the bug: an escalated receipt is never labelled with the rule that
    # widened it, so a straight rule_id comparison could never see it recur.
    predicate = {"rule_id": "R4", "key": "max_percent", "before": 2.9, "after": 3.2}
    matched_under_r4 = Decision(
        str(_receipt().id), "matched", "R4", "exact", evidence={"gap_percent": 3.1}
    )
    assert _matches(predicate, _receipt(), matched_under_r4)

    escalated_under_r8 = Decision(
        str(_receipt().id),
        "escalated",
        "R8",
        "exact",
        reason_code=ReasonCode.SHORT_PAY,
        evidence={"gap_percent": 3.1},
    )
    assert _matches(predicate, _receipt(), escalated_under_r8)

    outside_the_band = Decision(
        str(_receipt().id), "escalated", "R8", "exact", evidence={"gap_percent": 4.0}
    )
    assert not _matches(predicate, _receipt(), outside_the_band)

    no_evidence = Decision(str(_receipt().id), "matched", "R4", "exact")
    assert not _matches(predicate, _receipt(), no_evidence)

    assert not _matches(
        {"rule_id": "R3", "key": "threshold_centavos", "before": 500, "after": 600},
        _receipt(),
        matched_under_r4,
    )


def test_fixed_fee_condition_does_not_crash_when_percent_is_absent():
    # A fixed-centavo fee has no percent (review fix: this used to store
    # shortfall_pct=None and crash the next run's recurrence check).
    receipt = _receipt()
    decision = Decision(
        str(receipt.id),
        "escalated",
        "R1",
        "exact",
        reason_code=ReasonCode.SHORT_PAY,
        evidence={"gap_centavos": 300},
    )
    predicate = {"payment_type": "credit_card", "fee_fixed_centavos": 300, "tol_centavos": 1}
    assert _matches(predicate, receipt, decision)
    assert not _matches({**predicate, "fee_fixed_centavos": 500}, receipt, decision)
