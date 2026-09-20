"""Loads rows, calls the pure rule engine, writes every result in one transaction.
Spec 3.4 step 2 + review fix 1/5: Allocation/Adjustment/UnappliedCredit/ExceptionRecord/
LedgerEvent per decision, invariants checked over the resulting state, any failure rolls
back the whole run."""

import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.models import Adjustment as AdjustmentModel
from api.models import (
    AdjustmentReason,
    Counterparty,
    CounterpartyAlias,
    DecisionOutcome,
    ExceptionRecord,
    ExceptionStatus,
    LedgerEvent,
    PolicyVersion,
    ReceivableStatus,
    Run,
    RunReport,
    UnappliedCredit,
    UnappliedCreditStatus,
)
from api.models import Allocation as AllocationModel
from api.models import Decision as DecisionModel
from api.models import ReasonCode as DbReasonCode
from api.models import Receipt as ReceiptModel
from api.models import Receivable as ReceivableModel
from api.services.batches import ensure_batch_loaded
from api.services.recurrence import record_recurrence_samples
from finrecur.invariants import (
    LedgerAdjustment,
    LedgerAllocation,
    LedgerReceipt,
    LedgerReceivable,
    LedgerState,
    check,
)
from finrecur.reports import build_run_report
from finrecur.rules.engine import run_rules
from finrecur.rules.types import Decision
from finrecur.schema import Receipt, Receivable

BATCH_LINEAGE = {"day1": ("day1",), "day2": ("day1", "day2")}


class RunFailed(Exception):
    """Raised when post-run invariants fail. The caller's transaction is already
    rolled back by the time this is raised."""


def _load_policy_version(session: Session, version: int | None) -> PolicyVersion:
    stmt = select(PolicyVersion)
    stmt = stmt.where(PolicyVersion.version == version) if version is not None else stmt
    stmt = stmt.order_by(PolicyVersion.version.desc())
    row = session.scalars(stmt).first()
    if row is None:
        raise ValueError(
            f"policy version {version} not found" if version is not None else "no policy seeded"
        )
    return row


def _to_core_receivable(row: ReceivableModel) -> Receivable:
    return Receivable(
        id=str(row.id),
        counterparty_id=str(row.counterparty_id),
        total=row.total,
        shipping=row.shipping,
        issued_at=row.issued_at,
        status=row.status.value,
        source=row.source,
        simulated=row.simulated,
        meta=row.meta,
    )


def _to_core_receipt(row: ReceiptModel) -> Receipt:
    return Receipt(
        id=str(row.id),
        counterparty_id=str(row.counterparty_id),
        amount=row.amount,
        received_at=row.received_at,
        reference=row.reference,
        original_reference=row.original_reference,
        memo=row.memo,
        simulated=row.simulated,
        duplicate_of=str(row.duplicate_of) if row.duplicate_of else None,
        meta=row.meta,
    )


def build_aliases(session: Session) -> dict[str, str]:
    """counterparty_id -> canonical counterparty_id, resolved through
    CounterpartyAlias.alias_external_id (the variant's own external_id)."""
    by_external_id = {c.external_id: c.id for c in session.scalars(select(Counterparty)).all()}
    aliases: dict[str, str] = {}
    for a in session.scalars(select(CounterpartyAlias)).all():
        variant_id = by_external_id.get(a.alias_external_id)
        if variant_id is not None:
            aliases[str(variant_id)] = str(a.canonical_id)
    return aliases


def execute_run(session: Session, batch_label: str, policy_version: int | None = None) -> Run:
    holder: dict[str, Run] = {}
    for _ in execute_run_stream(session, batch_label, policy_version, _result_holder=holder):
        pass
    return holder["run"]


def execute_run_stream(
    session: Session,
    batch_label: str,
    policy_version: int | None = None,
    _result_holder: dict[str, Run] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yields one dict per receipt decision as it's produced, then commits everything
    atomically at the end. `execute_run` is a thin wrapper that drains this generator."""
    started = time.perf_counter()
    ensure_batch_loaded(session, batch_label)

    pv_row = _load_policy_version(session, policy_version)
    policy = pv_row.rules
    batches_included = list(BATCH_LINEAGE[batch_label])

    receivable_rows = list(
        session.scalars(
            select(ReceivableModel).where(
                ReceivableModel.meta["batch"].astext.in_(batches_included)
            )
        ).all()
    )
    receipt_rows = list(
        session.scalars(
            select(ReceiptModel).where(ReceiptModel.meta["batch"].astext.in_(batches_included))
        ).all()
    )
    counterparty_names = {c.id: c.name for c in session.scalars(select(Counterparty)).all()}
    aliases = build_aliases(session)

    core_receivables = [_to_core_receivable(r) for r in receivable_rows]
    core_receipts = [_to_core_receipt(r) for r in receipt_rows]
    decisions: list[Decision] = run_rules(core_receivables, core_receipts, aliases, policy)

    receivable_by_id = {str(r.id): r for r in receivable_rows}
    receipt_by_id = {str(r.id): r for r in receipt_rows}

    touched_receivable_ids = {rv_id for d in decisions for rv_id, _ in d.allocations}
    touched_receivable_ids |= {rv_id for d in decisions for rv_id, _, _ in d.adjustments}
    for rv_id in touched_receivable_ids:
        row = receivable_by_id[rv_id]
        row.allocated = 0
        row.adjusted = 0
        row.remaining = row.total

    run = Run(
        policy_version_id=pv_row.id,
        batch_label=batch_label,
        counts={},
        created_at=datetime.now(UTC),
    )
    session.add(run)
    session.flush()

    now = datetime.now(UTC)
    matched = 0
    escalated = 0
    applied_centavos = 0
    written_off_centavos = 0
    ledger_allocations: list[LedgerAllocation] = []
    ledger_adjustments: list[LedgerAdjustment] = []
    report_decisions: list[dict[str, Any]] = []

    # Rows that point at a decision by FK column are collected and added after one
    # flush of the decisions; a flush per receipt was the bottleneck on Docker's volume.
    deferred: list[Any] = []
    for d in decisions:
        receipt_row = receipt_by_id[d.receipt_id]
        outcome_model = (
            DecisionOutcome.SETTLED if d.outcome == "matched" else DecisionOutcome.ESCALATED
        )
        decision_row = DecisionModel(
            id=uuid.uuid4(),  # client-side so no flush per row (Docker volume I/O is slow)
            run_id=run.id,
            receipt_id=receipt_row.id,
            outcome=outcome_model,
            rule_id=d.rule_id,
            policy_version_id=pv_row.id,
            evidence=d.evidence,
            matched_by=d.matched_by,
            created_at=now,
        )
        session.add(decision_row)

        for rv_id, amount in d.allocations:
            rv_row = receivable_by_id[rv_id]
            alloc_id = uuid.uuid4()
            deferred.append(
                AllocationModel(
                    id=alloc_id,
                    receipt_id=receipt_row.id,
                    receivable_id=rv_row.id,
                    amount=amount,
                    decision_id=decision_row.id,
                    created_at=now,
                )
            )
            rv_row.allocated += amount
            applied_centavos += amount
            ledger_allocations.append(
                LedgerAllocation(
                    receipt_id=str(receipt_row.id), receivable_id=str(rv_row.id), amount=amount
                )
            )
            deferred.append(
                LedgerEvent(
                    actor="agent",
                    action="allocate",
                    entity_type="allocation",
                    entity_id=alloc_id,
                    after={
                        "receipt_id": str(receipt_row.id),
                        "receivable_id": str(rv_row.id),
                        "amount": amount,
                    },
                    created_at=now,
                )
            )

        for rv_id, amount, reason in d.adjustments:
            rv_row = receivable_by_id[rv_id]
            adj_id = uuid.uuid4()
            deferred.append(
                AdjustmentModel(
                    id=adj_id,
                    receivable_id=rv_row.id,
                    amount=amount,
                    reason=AdjustmentReason(reason),
                    decision_id=decision_row.id,
                    created_at=now,
                )
            )
            rv_row.adjusted += amount
            written_off_centavos += amount
            ledger_adjustments.append(
                LedgerAdjustment(receivable_id=str(rv_row.id), amount=amount, reason=reason)
            )
            deferred.append(
                LedgerEvent(
                    actor="agent",
                    action="adjust",
                    entity_type="adjustment",
                    entity_id=adj_id,
                    after={"receivable_id": str(rv_row.id), "amount": amount, "reason": reason},
                    created_at=now,
                )
            )

        if d.unapplied_credit:
            credit_id = uuid.uuid4()
            deferred.append(
                UnappliedCredit(
                    id=credit_id,
                    counterparty_id=receipt_row.counterparty_id,
                    receipt_id=receipt_row.id,
                    amount=d.unapplied_credit,
                    status=UnappliedCreditStatus.HELD,
                    created_at=now,
                )
            )
            deferred.append(
                LedgerEvent(
                    actor="agent",
                    action="unapplied_credit",
                    entity_type="unapplied_credit",
                    entity_id=credit_id,
                    after={"receipt_id": str(receipt_row.id), "amount": d.unapplied_credit},
                    created_at=now,
                )
            )

        exception_id: uuid.UUID | None = None
        if d.outcome == "escalated":
            escalated += 1
            assert d.reason_code is not None
            exception_row = ExceptionRecord(
                id=uuid.uuid4(),
                decision_id=decision_row.id,
                reason_code=DbReasonCode(d.reason_code.value),
                evidence=d.evidence,
                status=ExceptionStatus.OPEN,
                created_at=now,
            )
            deferred.append(exception_row)
            exception_id = exception_row.id
        else:
            matched += 1

        report_decisions.append(
            {
                "decision_id": str(decision_row.id),
                "outcome": outcome_model.value,
                "reason_code": d.reason_code.value if d.reason_code else None,
                "adjustments": [
                    {"amount": amount, "reason": reason} for _, amount, reason in d.adjustments
                ],
                "exception_id": str(exception_id) if exception_id else None,
            }
        )

        yield {
            "receipt_id": str(receipt_row.id),
            "counterparty_name": counterparty_names.get(receipt_row.counterparty_id),
            "amount": receipt_row.amount,
            "outcome": d.outcome,
            "rule_id": d.rule_id,
            "matched_by": d.matched_by,
            "reason_code": d.reason_code.value if d.reason_code else None,
        }

    session.flush()
    session.add_all(deferred)

    for rv_id in touched_receivable_ids:
        row = receivable_by_id[rv_id]
        row.remaining = row.total - row.allocated - row.adjusted
        if row.status == ReceivableStatus.CANCELLED:
            continue
        if row.remaining <= 0:
            row.status = ReceivableStatus.SETTLED
        elif row.allocated > 0 or row.adjusted > 0:
            row.status = ReceivableStatus.PARTIALLY_PAID
        else:
            row.status = ReceivableStatus.OPEN

    session.flush()

    ledger_state = LedgerState(
        receipts=[
            LedgerReceipt(id=str(r.id), counterparty_id=str(r.counterparty_id), amount=r.amount)
            for r in receipt_rows
        ],
        receivables=[
            LedgerReceivable(
                id=str(r.id),
                counterparty_id=str(r.counterparty_id),
                total=r.total,
                allocated=r.allocated,
                adjusted=r.adjusted,
                remaining=r.remaining,
            )
            for r in receivable_rows
        ],
        allocations=ledger_allocations,
        adjustments=ledger_adjustments,
        aliases=aliases,
    )
    results = check(ledger_state)
    failures = [r for r in results if not r.passed]
    if failures:
        session.rollback()
        raise RunFailed("; ".join(f"{r.name}: {r.detail}" for r in failures))

    run.duration_ms = int((time.perf_counter() - started) * 1000)
    run.counts = {
        "matched": matched,
        "escalated": escalated,
        "applied_centavos": applied_centavos,
        "written_off_centavos": written_off_centavos,
        "total_receipts": len(decisions),
    }
    record_recurrence_samples(session, run, receipt_rows, decisions)
    session.flush()

    report = build_run_report(
        run_id=str(run.id),
        policy_version=pv_row.version,
        batch_label=batch_label,
        decisions=report_decisions,
        invariant_results=[
            {"name": r.name, "passed": r.passed, "detail": r.detail} for r in results
        ],
    )
    session.add(
        RunReport(
            run_id=run.id,
            report=report,
            report_hash=report["report_hash"],
            created_at=now,
        )
    )

    session.commit()
    session.refresh(run)
    if _result_holder is not None:
        _result_holder["run"] = run
