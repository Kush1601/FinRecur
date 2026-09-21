"""Loads rows, calls the pure rule engine, writes every result in one transaction.
Spec 3.4 step 2 + review fix 1/5: Allocation/Adjustment/UnappliedCredit/ExceptionRecord/
LedgerEvent per decision, invariants checked over the resulting state, any failure rolls
back the whole run."""

import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from api.models import Adjustment as AdjustmentModel
from api.models import (
    AdjustmentReason,
    ClusterMember,
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
from api.services.ledger_state import BATCH_LINEAGE, current_ledger_state
from api.services.recurrence import record_recurrence_samples
from finrecur.invariants import check
from finrecur.reports import build_run_report
from finrecur.rules.engine import run_rules
from finrecur.rules.types import Decision
from finrecur.schema import Receipt, Receivable


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
    # The pure rule engine has no concept of "already partially paid" or
    # "settled" -- it always recomputes a receivable's outcome from scratch
    # (remaining starts at total). Those two statuses are what THIS layer
    # derives from a run's own output, not something the engine should read
    # back as input: R2's "is this receivable a candidate" check treats
    # anything other than status=="open" as unavailable, so a receivable this
    # run itself settled on a previous pass would wrongly stop being an R2
    # candidate on the next one, even though nothing about it changed.
    status = "cancelled" if row.status == ReceivableStatus.CANCELLED else "open"
    return Receivable(
        id=str(row.id),
        counterparty_id=str(row.counterparty_id),
        total=row.total,
        shipping=row.shipping,
        issued_at=row.issued_at,
        status=status,
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


def _is_clustered(session: Session, decision_id) -> bool:
    """True if a ClusterMember already points at this decision's open exception
    or adjustment -- grouping (step 4a) ran before any fix touched it. Those rows
    carry a live foreign key from ClusterMember, so a routine re-run must not
    delete them out from under the cluster; only the fix/manual-resolve path is
    allowed to change a decision from here."""
    return (
        session.scalar(
            select(ClusterMember.id)
            .outerjoin(ExceptionRecord, ClusterMember.exception_id == ExceptionRecord.id)
            .outerjoin(AdjustmentModel, ClusterMember.adjustment_id == AdjustmentModel.id)
            .where(
                or_(
                    ExceptionRecord.decision_id == decision_id,
                    AdjustmentModel.decision_id == decision_id,
                )
            )
            .limit(1)
        )
        is not None
    )


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
    atomically at the end. `execute_run` is a thin wrapper that drains this generator.

    Idempotent and incremental (review fix, 2026-09-21): every receipt in the
    batch's scope is re-decided every run (day2 replays day1 too, since a widened
    policy or a repaired reference can settle an old receipt), but a receipt whose
    decision hasn't changed is left untouched, and one already reviewed -- by an
    applied fix or a human resolving its exception -- or already grouped into a
    cluster is never silently overwritten. Only a receipt whose outcome actually
    differs, and that nothing downstream has touched yet, gets its old
    Allocation/Adjustment/Exception/UnappliedCredit rows reversed and replaced.
    Before this, every run inserted a fresh set of these rows for every receipt in
    scope regardless of what was already there, so a day2 run -- which by design
    reprocesses day1's receipts -- duplicated day1's allocations on top of itself.
    """
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
    touched_receivable_ids: set[str] = set()
    report_decisions: list[dict[str, Any]] = []

    existing_by_receipt: dict[uuid.UUID, DecisionModel] = {
        row.receipt_id: row
        for row in session.scalars(
            select(DecisionModel).where(DecisionModel.receipt_id.in_([r.id for r in receipt_rows]))
        ).all()
    }
    existing_decision_ids = [row.id for row in existing_by_receipt.values()]
    existing_alloc_sum: dict[uuid.UUID, int] = {}
    existing_adj_sum: dict[uuid.UUID, int] = {}
    if existing_decision_ids:
        # Postgres SUM() over a bigint column comes back as NUMERIC (Decimal in
        # psycopg) -- cast to int, or it taints run.counts (int arithmetic
        # with a Decimal) and JSONB serialization of the count fails.
        existing_alloc_sum = {
            decision_id: int(total)
            for decision_id, total in session.execute(
                select(AllocationModel.decision_id, func.sum(AllocationModel.amount))
                .where(AllocationModel.decision_id.in_(existing_decision_ids))
                .group_by(AllocationModel.decision_id)
            ).all()
            if decision_id is not None
        }
        existing_adj_sum = {
            decision_id: int(total)
            for decision_id, total in session.execute(
                select(AdjustmentModel.decision_id, func.sum(AdjustmentModel.amount))
                .where(AdjustmentModel.decision_id.in_(existing_decision_ids))
                .group_by(AdjustmentModel.decision_id)
            ).all()
            if decision_id is not None
        }

    def _tally_from_existing(row: DecisionModel) -> None:
        nonlocal matched, escalated, applied_centavos, written_off_centavos
        # An overpayment (R6) is outcome=escalated but still carries a real
        # allocation for the matched portion -- count the money on every
        # existing decision, not only the settled ones, matching what the
        # allocation/adjustment loops below do for a new or re-decided receipt.
        applied_centavos += existing_alloc_sum.get(row.id, 0)
        written_off_centavos += existing_adj_sum.get(row.id, 0)
        if row.outcome == DecisionOutcome.SETTLED:
            matched += 1
        else:
            escalated += 1

    # Rows that point at a decision by FK column are collected and added after the
    # decisions themselves are flushed -- a flush per receipt was the bottleneck on
    # Docker's volume, but Decision/Adjustment/Allocation/Exception have no ORM
    # relationship() between them (plain FK columns), so a single add_all() of a
    # large mixed batch does not reliably order the INSERTs by table dependency.
    # Two lists, two flushes: every decision exists before anything that points at it.
    new_decisions: list[DecisionModel] = []
    deferred: list[Any] = []
    for d in decisions:
        receipt_row = receipt_by_id[d.receipt_id]
        outcome_model = (
            DecisionOutcome.SETTLED if d.outcome == "matched" else DecisionOutcome.ESCALATED
        )
        existing = existing_by_receipt.get(receipt_row.id)
        # Same outcome/rule_id AND the same money moved -- a settle that split a
        # group's allocation differently between two runs (same outcome, same
        # rule, different amounts) must still go through the reversal-and-
        # rewrite path below, or the persisted total would silently drift.
        d_applied = sum(amount for _, amount in d.allocations)
        d_written_off = sum(amount for _, amount, _ in d.adjustments)
        unchanged = (
            existing is not None
            and existing.outcome == outcome_model
            and existing.rule_id == d.rule_id
            and existing_alloc_sum.get(existing.id, 0) == d_applied
            and existing_adj_sum.get(existing.id, 0) == d_written_off
        )
        already_reviewed = existing is not None and existing.reviewed_by is not None
        # A decision nobody has reviewed yet can still be grouped into a cluster
        # (step 4a runs before any fix does). Its exception/adjustment carries a
        # live foreign key from ClusterMember, so leave it alone the same way.
        clustered = (
            existing is not None
            and not unchanged
            and not already_reviewed
            and _is_clustered(session, existing.id)
        )

        if unchanged or already_reviewed or clustered:
            assert existing is not None
            _tally_from_existing(existing)
            report_decisions.append(
                {
                    "decision_id": str(existing.id),
                    "outcome": existing.outcome.value,
                    "reason_code": d.reason_code.value if d.reason_code else None,
                    "adjustments": [],
                    "exception_id": None,
                }
            )
            yield {
                "receipt_id": str(receipt_row.id),
                "counterparty_name": counterparty_names.get(receipt_row.counterparty_id),
                "amount": receipt_row.amount,
                "outcome": (
                    "matched" if existing.outcome == DecisionOutcome.SETTLED else "escalated"
                ),
                "rule_id": existing.rule_id,
                "matched_by": existing.matched_by,
                "reason_code": d.reason_code.value if d.reason_code else None,
            }
            continue

        if existing is not None:
            # Re-decided: reverse whatever the old decision produced before
            # applying the new one on the same row, instead of layering on top.
            for a in session.scalars(
                select(AllocationModel).where(AllocationModel.decision_id == existing.id)
            ).all():
                rv = receivable_by_id.get(str(a.receivable_id))
                if rv is not None:
                    rv.allocated -= a.amount
                    touched_receivable_ids.add(str(rv.id))
                session.delete(a)
            for a in session.scalars(
                select(AdjustmentModel).where(AdjustmentModel.decision_id == existing.id)
            ).all():
                rv = receivable_by_id.get(str(a.receivable_id))
                if rv is not None:
                    rv.adjusted -= a.amount
                    touched_receivable_ids.add(str(rv.id))
                session.delete(a)
            old_exception = session.scalars(
                select(ExceptionRecord).where(
                    ExceptionRecord.decision_id == existing.id,
                    ExceptionRecord.status == ExceptionStatus.OPEN,
                )
            ).first()
            if old_exception is not None:
                session.delete(old_exception)
            for credit in session.scalars(
                select(UnappliedCredit).where(
                    UnappliedCredit.receipt_id == receipt_row.id,
                    UnappliedCredit.status == UnappliedCreditStatus.HELD,
                )
            ).all():
                session.delete(credit)
            session.flush()
            decision_row = existing
            decision_row.run_id = run.id
            decision_row.outcome = outcome_model
            decision_row.rule_id = d.rule_id
            decision_row.matched_by = d.matched_by
            decision_row.evidence = d.evidence
            decision_row.policy_version_id = pv_row.id
        else:
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
            new_decisions.append(decision_row)

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
            touched_receivable_ids.add(rv_id)
            applied_centavos += amount
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
            touched_receivable_ids.add(rv_id)
            written_off_centavos += amount
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

    session.add_all(new_decisions)
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

    ledger_state = current_ledger_state(session, run, build_aliases)
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
