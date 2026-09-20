"""Post-run ledger invariants, pure. Spec 3.4 review fix 1: run after every apply;
any failure rolls back the whole transaction. Inputs are plain dataclasses so a test
can build a `LedgerState` from literal fixtures without touching SQLAlchemy."""

from collections import defaultdict
from dataclasses import dataclass, field

KNOWN_ADJUSTMENT_REASONS = {"rounding", "short_pay", "tolerance", "fee"}


@dataclass(frozen=True)
class LedgerReceipt:
    id: str
    counterparty_id: str
    amount: int


@dataclass(frozen=True)
class LedgerReceivable:
    id: str
    counterparty_id: str
    total: int
    allocated: int
    adjusted: int
    remaining: int


@dataclass(frozen=True)
class LedgerAllocation:
    receipt_id: str
    receivable_id: str
    amount: int


@dataclass(frozen=True)
class LedgerAdjustment:
    receivable_id: str
    amount: int
    reason: str | None


@dataclass(frozen=True)
class LedgerState:
    receipts: list[LedgerReceipt]
    receivables: list[LedgerReceivable]
    allocations: list[LedgerAllocation]
    adjustments: list[LedgerAdjustment]
    aliases: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class InvariantResult:
    name: str
    passed: bool
    detail: str | None = None


def _canonical(counterparty_id: str, aliases: dict[str, str]) -> str:
    return aliases.get(counterparty_id, counterparty_id)


def _no_receipt_over_allocated(state: LedgerState) -> InvariantResult:
    receipts_by_id = {r.id: r for r in state.receipts}
    by_receipt: dict[str, int] = defaultdict(int)
    for a in state.allocations:
        by_receipt[a.receipt_id] += a.amount
    offenders = [
        rid
        for rid, total in by_receipt.items()
        if rid in receipts_by_id and total > receipts_by_id[rid].amount
    ]
    return InvariantResult(
        "no_receipt_over_allocated",
        not offenders,
        f"receipts allocated beyond their amount: {offenders}" if offenders else None,
    )


def _no_receivable_over_allocated(state: LedgerState) -> InvariantResult:
    offenders = [rv.id for rv in state.receivables if rv.allocated + rv.adjusted > rv.total]
    return InvariantResult(
        "no_receivable_over_allocated",
        not offenders,
        f"receivables allocated+adjusted beyond total: {offenders}" if offenders else None,
    )


def _no_duplicate_allocation(state: LedgerState) -> InvariantResult:
    seen: dict[tuple[str, str], int] = defaultdict(int)
    for a in state.allocations:
        seen[(a.receipt_id, a.receivable_id)] += 1
    offenders = [pair for pair, count in seen.items() if count > 1]
    return InvariantResult(
        "no_duplicate_allocation",
        not offenders,
        f"same receipt/receivable pair allocated more than once: {offenders}"
        if offenders
        else None,
    )


def _counterparty_agreement(state: LedgerState) -> InvariantResult:
    receipts_by_id = {r.id: r for r in state.receipts}
    receivables_by_id = {rv.id: rv for rv in state.receivables}
    offenders = []
    for a in state.allocations:
        receipt = receipts_by_id.get(a.receipt_id)
        receivable = receivables_by_id.get(a.receivable_id)
        if receipt is None or receivable is None:
            continue
        if _canonical(receipt.counterparty_id, state.aliases) != _canonical(
            receivable.counterparty_id, state.aliases
        ):
            offenders.append((a.receipt_id, a.receivable_id))
    return InvariantResult(
        "counterparty_agreement",
        not offenders,
        f"allocation crosses counterparties (not alias-linked): {offenders}" if offenders else None,
    )


def _balance_consistency(state: LedgerState) -> InvariantResult:
    offenders = [
        rv.id for rv in state.receivables if rv.allocated + rv.adjusted + rv.remaining != rv.total
    ]
    return InvariantResult(
        "balance_consistency",
        not offenders,
        f"allocated+adjusted+remaining != total: {offenders}" if offenders else None,
    )


def _every_adjustment_has_reason(state: LedgerState) -> InvariantResult:
    offenders = [
        i
        for i, adj in enumerate(state.adjustments)
        if not adj.reason or adj.reason not in KNOWN_ADJUSTMENT_REASONS
    ]
    return InvariantResult(
        "every_adjustment_has_reason",
        not offenders,
        f"adjustments with no/unknown reason at index: {offenders}" if offenders else None,
    )


CHECKS = (
    _no_receipt_over_allocated,
    _no_receivable_over_allocated,
    _no_duplicate_allocation,
    _counterparty_agreement,
    _balance_consistency,
    _every_adjustment_has_reason,
)


def check(state: LedgerState) -> list[InvariantResult]:
    return [fn(state) for fn in CHECKS]
