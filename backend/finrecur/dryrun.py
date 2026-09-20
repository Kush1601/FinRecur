"""Dry run (spec 3.4 step 6, 3.9). Applies a fix to a copy of the state and
predicts the outcome; for the fix types the rule engine can represent as a change
to its own inputs (alias table, a receipt's reference, a receipt's duplicate flag,
the policy dict) it re-runs R1-R8 and diffs every decision, so a receipt outside
the cluster whose outcome flips shows up as a side effect. `split_receipt` and
`record_fee_deduction` settle specific receipts directly (one receipt paying many
receivables, or a fee the rule engine has no vocabulary for) -- their predicted
counts are read straight off the state diff instead. Side effects are worded as
"additional write-offs you'd be accepting", never "errors" (review fix 4)."""

from dataclasses import dataclass

from finrecur.fixes.engine import State, apply_to_state
from finrecur.rules.engine import run_rules
from finrecur.rules.types import Decision
from finrecur.schema import Receipt, Receivable

RERUN_FIX_TYPES = {"add_counterparty_alias", "repair_reference", "mark_duplicate", "amend_policy"}


@dataclass(frozen=True)
class SideEffect:
    receipt_id: str
    counterparty_id: str
    amount_centavos: int
    would_become: str
    write_off_centavos: int


@dataclass(frozen=True)
class DryRunPrediction:
    matched: int
    still_failing: int
    applied_centavos: int
    written_off_centavos: int


@dataclass(frozen=True)
class DryRunResult:
    predicted: DryRunPrediction
    side_effects: tuple[SideEffect, ...]
    affected_receipt_ids: tuple[str, ...]


def _to_schema_receivable(rv) -> Receivable:
    return Receivable(
        id=rv.id,
        counterparty_id=rv.counterparty_id,
        total=rv.total,
        shipping=rv.shipping,
        issued_at=rv.issued_at,
        status=rv.status,
        meta=rv.meta,
    )


def _to_schema_receipt(r) -> Receipt:
    return Receipt(
        id=r.id,
        counterparty_id=r.counterparty_id,
        amount=r.amount,
        received_at=r.received_at,
        reference=r.reference,
        original_reference=r.original_reference,
        duplicate_of=r.duplicate_of,
        meta=r.meta,
    )


def decide(state: State) -> dict[str, Decision]:
    """Full rerun of R1-R8 over every non-duplicate receipt in the state. A
    receipt marked duplicate_of by a mark_duplicate fix is excluded -- the rule
    engine has no concept of "ignore this receipt", so it is treated here as
    settled by the fix itself, not run through matching."""
    receivables = [_to_schema_receivable(rv) for rv in state.receivables.values()]
    receipts = [_to_schema_receipt(r) for r in state.receipts.values() if r.duplicate_of is None]
    decisions = run_rules(receivables, receipts, state.aliases, state.policy)
    return {d.receipt_id: d for d in decisions}


def _outcome_key(d: Decision) -> tuple:
    return (d.outcome, d.rule_id, d.reason_code)


def _describe(d: Decision) -> str:
    if d.outcome == "matched":
        return f"auto-settled ({d.rule_id})"
    return f"escalated ({d.reason_code.value if d.reason_code else d.rule_id})"


def _direct_prediction(
    fix_type: str, params: dict, before: State, after: State
) -> DryRunPrediction:
    """split_receipt / record_fee_deduction settle receipts by direct allocation,
    not by anything the rule engine can rerun -- read the effect off the state
    diff instead of a re-decided outcome."""
    if fix_type == "split_receipt":
        applied = sum(a["amount"] for s in params["splits"] for a in s["allocations"])
        return DryRunPrediction(
            matched=len(params["splits"]),
            still_failing=0,
            applied_centavos=applied,
            written_off_centavos=0,
        )

    receipt_ids = params["receipt_ids"]
    before_adjusted = {rv.id: rv.adjusted for rv in before.receivables.values()}
    fee_total = sum(
        rv.adjusted - before_adjusted.get(rv_id, 0) for rv_id, rv in after.receivables.items()
    )
    applied = sum(before.receipts[rid].amount for rid in receipt_ids)
    return DryRunPrediction(
        matched=len(receipt_ids),
        still_failing=0,
        applied_centavos=applied,
        written_off_centavos=fee_total,
    )


def dry_run(
    fix_type: str,
    params: dict,
    state: State,
    affected_receipt_ids: list[str],
    receipt_receivable_hint: dict[str, str] | None = None,
) -> DryRunResult:
    after_state, _inverse = apply_to_state(fix_type, params, state, receipt_receivable_hint)
    affected = set(affected_receipt_ids)

    if fix_type not in RERUN_FIX_TYPES:
        prediction = _direct_prediction(fix_type, params, state, after_state)
        return DryRunResult(prediction, (), tuple(sorted(affected)))

    before_decisions = decide(state)
    after_decisions = decide(after_state)

    matched = still_failing = applied = written_off = 0
    for rid in affected:
        d = after_decisions.get(rid)
        if d is None:  # e.g. now marked duplicate_of and excluded from the rerun
            matched += 1
            continue
        if d.outcome == "matched":
            matched += 1
            applied += sum(amt for _, amt in d.allocations)
            written_off += sum(amt for _, amt, _ in d.adjustments)
        else:
            still_failing += 1

    side_effects = []
    for rid, before_d in before_decisions.items():
        if rid in affected:
            continue
        after_d = after_decisions.get(rid)
        if after_d is None or _outcome_key(before_d) == _outcome_key(after_d):
            continue
        receipt = state.receipts[rid]
        side_effects.append(
            SideEffect(
                receipt_id=rid,
                counterparty_id=receipt.counterparty_id,
                amount_centavos=receipt.amount,
                would_become=_describe(after_d),
                write_off_centavos=sum(amt for _, amt, _ in after_d.adjustments),
            )
        )

    prediction = DryRunPrediction(matched, still_failing, applied, written_off)
    return DryRunResult(prediction, tuple(side_effects), tuple(sorted(affected)))
