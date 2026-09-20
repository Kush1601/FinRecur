"""Evaluate stored condition signatures against a run's incoming batch.

The run engine may replay earlier rows to compute balances, but recurrence only
counts receipts whose ``meta.batch`` equals the current run label. That keeps a
widened rule from making an old condition look as though it appeared again.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.models import ConditionSignature, Fix, Receipt, RecurrenceSample, Run
from finrecur.rules.types import Decision


def _matches(predicate: dict, receipt: Receipt, decision: Decision) -> bool:
    if "counterparty_id" in predicate:
        return str(receipt.counterparty_id) == predicate["counterparty_id"]
    if predicate.get("fingerprint_kind") == "amount_date_reference":
        return decision.reason_code is not None and decision.reason_code.value in {
            "duplicate_receipt",
            "suspected_duplicate",
        }
    if predicate.get("overpayment_multi_receivable") is True:
        return decision.reason_code is not None and decision.reason_code.value == "overpayment"
    if "reference_edit_distance" in predicate:
        return bool(receipt.original_reference) or (
            decision.matched_by == "amount_date" and bool(receipt.reference)
        )
    if "payment_type" in predicate and "shortfall_pct" in predicate:
        if receipt.meta.get("payment_type") != predicate["payment_type"]:
            return False
        actual = decision.evidence.get("gap_percent")
        if actual is None:
            return False
        return abs(float(actual) - float(predicate["shortfall_pct"])) <= float(
            predicate.get("tol", 0)
        )
    if "rule_id" in predicate:
        return decision.rule_id == predicate["rule_id"]
    return False


def record_recurrence_samples(
    session: Session,
    run: Run,
    receipt_rows: list[Receipt],
    decisions: list[Decision],
) -> None:
    signatures = session.scalars(select(ConditionSignature)).all()
    if not signatures:
        return

    incoming = {
        str(receipt.id): receipt
        for receipt in receipt_rows
        if receipt.meta.get("batch") == run.batch_label
    }
    decisions_by_receipt = {decision.receipt_id: decision for decision in decisions}
    now = datetime.now(UTC)

    for signature in signatures:
        # A signature starts watching after its owning fix was applied. The run that
        # produced the cluster is evidence for the fix, not a recurrence sample.
        fix = session.get(Fix, signature.fix_id)
        if fix is None:
            continue
        matched = [
            decision
            for receipt_id, receipt in incoming.items()
            if (decision := decisions_by_receipt.get(receipt_id)) is not None
            and _matches(signature.predicate, receipt, decision)
        ]
        session.add(
            RecurrenceSample(
                signature_id=signature.id,
                run_id=run.id,
                appeared=len(matched),
                auto_handled=sum(decision.outcome == "matched" for decision in matched),
                needed_human=sum(decision.outcome == "escalated" for decision in matched),
                created_at=now,
            )
        )
