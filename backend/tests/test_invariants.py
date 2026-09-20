"""One failing fixture per invariant, plus a fixture where everything holds."""

from finrecur.invariants import (
    LedgerAdjustment,
    LedgerAllocation,
    LedgerReceipt,
    LedgerReceivable,
    LedgerState,
    check,
)


def _results(state: LedgerState) -> dict[str, bool]:
    return {r.name: r.passed for r in check(state)}


def test_all_pass_on_a_consistent_state():
    state = LedgerState(
        receipts=[LedgerReceipt("r-1", "cp-1", 1000)],
        receivables=[LedgerReceivable("rv-1", "cp-1", 1000, 1000, 0, 0)],
        allocations=[LedgerAllocation("r-1", "rv-1", 1000)],
        adjustments=[],
    )
    results = _results(state)
    assert all(results.values()), results


def test_no_receipt_over_allocated_fails_when_allocation_exceeds_receipt_amount():
    state = LedgerState(
        receipts=[LedgerReceipt("r-1", "cp-1", 500)],
        receivables=[LedgerReceivable("rv-1", "cp-1", 1000, 1000, 0, 0)],
        allocations=[LedgerAllocation("r-1", "rv-1", 1000)],
        adjustments=[],
    )
    results = _results(state)
    assert results["no_receipt_over_allocated"] is False


def test_no_receivable_over_allocated_fails_when_allocated_plus_adjusted_exceeds_total():
    state = LedgerState(
        receipts=[LedgerReceipt("r-1", "cp-1", 1200)],
        receivables=[LedgerReceivable("rv-1", "cp-1", 1000, 900, 200, -100)],
        allocations=[LedgerAllocation("r-1", "rv-1", 900)],
        adjustments=[LedgerAdjustment("rv-1", 200, "short_pay")],
    )
    results = _results(state)
    assert results["no_receivable_over_allocated"] is False


def test_no_duplicate_allocation_fails_on_repeated_pair():
    state = LedgerState(
        receipts=[LedgerReceipt("r-1", "cp-1", 1000)],
        receivables=[LedgerReceivable("rv-1", "cp-1", 1000, 1000, 0, 0)],
        allocations=[
            LedgerAllocation("r-1", "rv-1", 500),
            LedgerAllocation("r-1", "rv-1", 500),
        ],
        adjustments=[],
    )
    results = _results(state)
    assert results["no_duplicate_allocation"] is False


def test_counterparty_agreement_fails_across_unlinked_counterparties():
    state = LedgerState(
        receipts=[LedgerReceipt("r-1", "cp-1", 1000)],
        receivables=[LedgerReceivable("rv-1", "cp-2", 1000, 1000, 0, 0)],
        allocations=[LedgerAllocation("r-1", "rv-1", 1000)],
        adjustments=[],
    )
    results = _results(state)
    assert results["counterparty_agreement"] is False


def test_counterparty_agreement_passes_when_alias_linked():
    state = LedgerState(
        receipts=[LedgerReceipt("r-1", "cp-variant", 1000)],
        receivables=[LedgerReceivable("rv-1", "cp-canonical", 1000, 1000, 0, 0)],
        allocations=[LedgerAllocation("r-1", "rv-1", 1000)],
        adjustments=[],
        aliases={"cp-variant": "cp-canonical"},
    )
    results = _results(state)
    assert results["counterparty_agreement"] is True


def test_balance_consistency_fails_when_parts_dont_sum_to_total():
    state = LedgerState(
        receipts=[LedgerReceipt("r-1", "cp-1", 1000)],
        receivables=[LedgerReceivable("rv-1", "cp-1", 1000, 500, 0, 400)],
        allocations=[LedgerAllocation("r-1", "rv-1", 500)],
        adjustments=[],
    )
    results = _results(state)
    assert results["balance_consistency"] is False


def test_every_adjustment_has_reason_fails_on_missing_reason():
    state = LedgerState(
        receipts=[LedgerReceipt("r-1", "cp-1", 1000)],
        receivables=[LedgerReceivable("rv-1", "cp-1", 1000, 900, 100, 0)],
        allocations=[LedgerAllocation("r-1", "rv-1", 900)],
        adjustments=[LedgerAdjustment("rv-1", 100, None)],
    )
    results = _results(state)
    assert results["every_adjustment_has_reason"] is False


def test_every_adjustment_has_reason_fails_on_unknown_reason():
    state = LedgerState(
        receipts=[LedgerReceipt("r-1", "cp-1", 1000)],
        receivables=[LedgerReceivable("rv-1", "cp-1", 1000, 900, 100, 0)],
        allocations=[LedgerAllocation("r-1", "rv-1", 900)],
        adjustments=[LedgerAdjustment("rv-1", 100, "made_up_reason")],
    )
    results = _results(state)
    assert results["every_adjustment_has_reason"] is False
