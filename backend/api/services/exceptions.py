"""Manual exception resolution (spec 3.7 screen 2 singletons tab, route contract
POST /exceptions/{id}/resolve). Distinct from the fix pipeline in api/services/fixes.py:
this is a one-off human action on a single exception, not a proposed/dry-run/approved
fix, so it writes directly and sets Decision.reviewed_by/at/outcome itself -- the one
place besides a fix apply that's allowed to."""

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from api.models import Adjustment as AdjustmentModel
from api.models import (
    AdjustmentReason,
    DecisionOutcome,
    ExceptionStatus,
    LedgerEvent,
    ReceivableStatus,
)
from api.models import Allocation as AllocationModel
from api.models import Decision as DecisionModel
from api.models import ExceptionRecord as ExceptionModel
from api.models import Receipt as ReceiptModel
from api.models import Receivable as ReceivableModel


class ResolveError(Exception):
    """Caller error (exception not found, missing receivable_id for an action that
    needs one) -- routes turn this into a 4xx."""


def _ledger(
    session: Session, actor: str, action: str, entity_type: str, entity_id, after: dict
) -> None:
    session.add(
        LedgerEvent(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            after=after,
            created_at=datetime.now(UTC),
        )
    )


def _recompute_status(rv: ReceivableModel) -> None:
    rv.remaining = rv.total - rv.allocated - rv.adjusted
    if rv.status == ReceivableStatus.CANCELLED:
        return
    rv.status = (
        ReceivableStatus.SETTLED
        if rv.remaining <= 0
        else ReceivableStatus.PARTIALLY_PAID
        if rv.allocated > 0 or rv.adjusted > 0
        else ReceivableStatus.OPEN
    )


def resolve_exception(
    session: Session,
    exception_id: str,
    action: str,
    receivable_id: str | None,
    note: str | None,
    name: str,
) -> ExceptionModel:
    exception = session.get(ExceptionModel, exception_id)
    if exception is None:
        raise ResolveError(f"exception {exception_id} not found")
    decision = session.get(DecisionModel, exception.decision_id)
    assert decision is not None
    receipt = session.get(ReceiptModel, decision.receipt_id)
    assert receipt is not None
    now = datetime.now(UTC)

    if action == "apply_to":
        if not receivable_id:
            raise ResolveError("apply_to needs receivable_id")
        rv = session.get(ReceivableModel, receivable_id)
        if rv is None:
            raise ResolveError(f"receivable {receivable_id} not found")
        amount = min(receipt.amount, rv.remaining)
        alloc_id = uuid.uuid4()
        session.add(
            AllocationModel(
                id=alloc_id,
                receipt_id=receipt.id,
                receivable_id=rv.id,
                amount=amount,
                decision_id=decision.id,
                created_at=now,
            )
        )
        rv.allocated += amount
        _recompute_status(rv)
        _ledger(
            session,
            name,
            "allocate",
            "allocation",
            alloc_id,
            {"receipt_id": str(receipt.id), "receivable_id": str(rv.id), "amount": amount},
        )
        decision.outcome = DecisionOutcome.SETTLED

    elif action == "write_off":
        target_id = receivable_id or (exception.evidence.get("candidate_receivables") or [{}])[
            0
        ].get("id")
        if not target_id:
            raise ResolveError("write_off needs receivable_id or a single evidenced candidate")
        rv = session.get(ReceivableModel, target_id)
        if rv is None:
            raise ResolveError(f"receivable {target_id} not found")
        amount = exception.evidence.get("gap_centavos") or receipt.amount
        adj_id = uuid.uuid4()
        session.add(
            AdjustmentModel(
                id=adj_id,
                receivable_id=rv.id,
                amount=amount,
                reason=AdjustmentReason.SHORT_PAY,
                decision_id=decision.id,
                approved_by=name,
                created_at=now,
            )
        )
        rv.adjusted += amount
        _recompute_status(rv)
        _ledger(
            session,
            name,
            "adjust",
            "adjustment",
            adj_id,
            {"receivable_id": str(rv.id), "amount": amount, "reason": "short_pay"},
        )
        decision.outcome = DecisionOutcome.SETTLED

    elif action == "mark_duplicate":
        if not receivable_id:
            raise ResolveError("mark_duplicate needs receivable_id (the earlier receipt's id)")
        receipt.duplicate_of = uuid.UUID(receivable_id)
        _ledger(
            session, name, "mark_duplicate", "receipt", receipt.id, {"duplicate_of": receivable_id}
        )

    elif action in ("reject", "leave_open"):
        pass

    else:
        raise ResolveError(f"unknown action {action}")

    exception.status = ExceptionStatus.OPEN if action == "leave_open" else ExceptionStatus.RESOLVED
    exception.resolved_by = name
    exception.resolution = note
    decision.reviewed_by = name
    decision.reviewed_at = now
    decision.review_outcome = action
    session.flush()
    _ledger(
        session,
        name,
        "resolve_exception",
        "exception",
        exception.id,
        {"action": action, "note": note},
    )
    session.commit()
    session.refresh(exception)
    return exception
