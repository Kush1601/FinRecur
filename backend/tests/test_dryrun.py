"""Dry-run side effects on a literal fixture (spec 3.4 step 9 test list): widening
R4.max_percent from 2.9 to 3.5 fixes the cluster's own receipt AND, as a side
effect, a second receipt outside the cluster whose shortfall now also fits."""

from datetime import UTC, datetime

from finrecur.dryrun import dry_run
from finrecur.fixes.engine import State, StateReceipt, StateReceivable

NOW = datetime(2026, 9, 1, tzinfo=UTC)
POLICY = {
    "R2": {"window_days": 30},
    "R3": {"threshold_centavos": 500},
    "R4": {"max_percent": 2.9, "must_fit_shipping": True},
    "R7": {"tolerance_centavos": 1},
}


def _receivable(id_, total, shipping) -> StateReceivable:
    return StateReceivable(
        id=id_,
        counterparty_id=f"cp-{id_}",
        total=total,
        shipping=shipping,
        allocated=0,
        adjusted=0,
        remaining=total,
        status="open",
        issued_at=NOW,
    )


def _receipt(id_, receivable_id, amount) -> StateReceipt:
    return StateReceipt(
        id=id_,
        counterparty_id=f"cp-{receivable_id}",
        amount=amount,
        received_at=NOW,
        reference=receivable_id,
    )


def _state() -> State:
    cluster_rv = _receivable("rv-cluster", 20000, shipping=700)
    side_rv = _receivable("rv-side", 20000, shipping=700)
    cluster_receipt = _receipt("r-cluster", "rv-cluster", 19350)  # shortfall 650, 3.25%
    side_receipt = _receipt("r-side", "rv-side", 19350)
    return State(
        receivables={"rv-cluster": cluster_rv, "rv-side": side_rv},
        receipts={"r-cluster": cluster_receipt, "r-side": side_receipt},
        allocations=[],
        adjustments=[],
        aliases={},
        policy=dict(POLICY),
    )


def test_widening_r4_fixes_cluster_and_reveals_a_side_effect():
    state = _state()
    params = {"rule_id": "R4", "key": "max_percent", "before": 2.9, "after": 3.5}
    result = dry_run("amend_policy", params, state, affected_receipt_ids=["r-cluster"])

    assert result.predicted.matched == 1
    assert result.predicted.still_failing == 0
    assert result.predicted.applied_centavos == 19350
    assert result.predicted.written_off_centavos == 650

    assert len(result.side_effects) == 1
    effect = result.side_effects[0]
    assert effect.receipt_id == "r-side"
    assert effect.write_off_centavos == 650
    assert "auto-settled" in effect.would_become


def test_no_side_effects_when_amendment_is_narrow_enough():
    state = _state()
    params = {"rule_id": "R4", "key": "max_percent", "before": 2.9, "after": 3.0}
    result = dry_run("amend_policy", params, state, affected_receipt_ids=["r-cluster"])

    # 3.25% still exceeds 3.0%, so neither receipt is rescued.
    assert result.predicted.still_failing == 1
    assert result.side_effects == ()


def test_split_receipt_predicts_directly_without_a_rerun():
    state = _state()
    state.receivables["rv-extra"] = _receivable("rv-extra", 5000, shipping=0)
    state.receipts["r-split"] = StateReceipt(
        id="r-split", counterparty_id="cp-rv-cluster", amount=25000, received_at=NOW
    )
    params = {
        "splits": [
            {
                "receipt_id": "r-split",
                "allocations": [
                    {"receivable_id": "rv-cluster", "amount": 20000},
                    {"receivable_id": "rv-extra", "amount": 5000},
                ],
            }
        ]
    }
    result = dry_run("split_receipt", params, state, affected_receipt_ids=["r-split"])
    assert result.predicted.matched == 1
    assert result.predicted.applied_centavos == 25000
    assert result.side_effects == ()
