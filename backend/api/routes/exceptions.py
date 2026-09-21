from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import get_db
from api.models import (
    ClusterMember,
    Counterparty,
    Decision,
    ExceptionRecord,
    ExceptionStatus,
    PolicyVersion,
    Receipt,
)
from api.services.exceptions import ResolveError, resolve_exception

router = APIRouter(tags=["exceptions"])


class ExceptionOut(BaseModel):
    id: str
    decision_id: str
    receipt_id: str
    source_receipt_id: str
    reference: str
    counterparty: str
    amount_centavos: int
    received_at: str
    payment_type: str | None
    customer_state: str | None
    seller_locations: list[dict[str, str]]
    simulated: bool
    reason_code: str
    evidence: dict
    status: str
    resolved_by: str | None
    resolution: str | None
    policy_version: int
    created_at: str


class Page(BaseModel):
    items: list[ExceptionOut]
    total: int


def _exception_out(
    record: ExceptionRecord,
    decision: Decision,
    receipt: Receipt,
    counterparty: Counterparty,
    policy_version: int,
) -> ExceptionOut:
    source_receipt_id = str(receipt.meta.get("source_id", receipt.id))
    return ExceptionOut(
        id=str(record.id),
        decision_id=str(record.decision_id),
        receipt_id=str(receipt.id),
        source_receipt_id=source_receipt_id,
        reference=receipt.original_reference or receipt.reference or source_receipt_id,
        counterparty=counterparty.name,
        amount_centavos=receipt.amount,
        received_at=receipt.received_at.isoformat(),
        payment_type=receipt.meta.get("payment_type"),
        customer_state=receipt.meta.get("customer_state"),
        seller_locations=receipt.meta.get("seller_locations", []),
        simulated=receipt.simulated,
        reason_code=record.reason_code.value,
        evidence=record.evidence,
        status=record.status.value,
        resolved_by=record.resolved_by,
        resolution=record.resolution,
        policy_version=policy_version,
        created_at=record.created_at.isoformat(),
    )


@router.get("/exceptions", response_model=Page)
def list_exceptions(
    status: ExceptionStatus | None = None,
    run_id: str | None = None,
    singletons_only: bool = False,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> Page:
    stmt = select(ExceptionRecord)
    if run_id is not None:
        stmt = stmt.join(Decision, ExceptionRecord.decision_id == Decision.id).where(
            Decision.run_id == run_id
        )
    if status is not None:
        stmt = stmt.where(ExceptionRecord.status == status)
    if singletons_only:
        grouped_ids = select(ClusterMember.exception_id).where(
            ClusterMember.exception_id.is_not(None)
        )
        stmt = stmt.where(ExceptionRecord.id.not_in(grouped_ids))

    total = len(db.scalars(stmt).all())
    rows = db.scalars(stmt.order_by(ExceptionRecord.created_at).limit(limit).offset(offset)).all()
    decisions = {
        row.id: row
        for row in db.scalars(
            select(Decision).where(Decision.id.in_([record.decision_id for record in rows]))
        ).all()
    }
    receipts = {
        row.id: row
        for row in db.scalars(
            select(Receipt).where(
                Receipt.id.in_([decision.receipt_id for decision in decisions.values()])
            )
        ).all()
    }
    counterparty_ids = {receipt.counterparty_id for receipt in receipts.values()}
    counterparties = {
        row.id: row
        for row in db.scalars(
            select(Counterparty).where(Counterparty.id.in_(counterparty_ids))
        ).all()
    }
    policy_ids = {row.policy_version_id for row in decisions.values()}
    policies = {
        row.id: row.version
        for row in db.scalars(select(PolicyVersion).where(PolicyVersion.id.in_(policy_ids))).all()
    }
    return Page(
        items=[
            _exception_out(
                r,
                decisions[r.decision_id],
                receipts[decisions[r.decision_id].receipt_id],
                counterparties[receipts[decisions[r.decision_id].receipt_id].counterparty_id],
                policies[decisions[r.decision_id].policy_version_id],
            )
            for r in rows
        ],
        total=total,
    )


class ResolveRequest(BaseModel):
    action: str
    receivable_id: str | None = None
    note: str | None = None
    name: str


@router.post("/exceptions/{exception_id}/resolve", response_model=ExceptionOut)
def resolve_exception_route(
    exception_id: str, body: ResolveRequest, db: Session = Depends(get_db)
) -> ExceptionOut:
    try:
        row = resolve_exception(
            db, exception_id, body.action, body.receivable_id, body.note, body.name
        )
    except ResolveError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    decision = db.get(Decision, row.decision_id)
    assert decision is not None
    policy = db.get(PolicyVersion, decision.policy_version_id)
    assert policy is not None
    receipt = db.get(Receipt, decision.receipt_id)
    assert receipt is not None
    counterparty = db.get(Counterparty, receipt.counterparty_id)
    assert counterparty is not None
    return _exception_out(row, decision, receipt, counterparty, policy.version)
