from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import get_db
from api.models import (
    ClusterMember,
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
    policy_ids = {row.policy_version_id for row in decisions.values()}
    policies = {
        row.id: row.version
        for row in db.scalars(select(PolicyVersion).where(PolicyVersion.id.in_(policy_ids))).all()
    }
    return Page(
        items=[
            ExceptionOut(
                id=str(r.id),
                decision_id=str(r.decision_id),
                receipt_id=str(decisions[r.decision_id].receipt_id),
                simulated=receipts[decisions[r.decision_id].receipt_id].simulated,
                reason_code=r.reason_code.value,
                evidence=r.evidence,
                status=r.status.value,
                resolved_by=r.resolved_by,
                resolution=r.resolution,
                policy_version=policies[decisions[r.decision_id].policy_version_id],
                created_at=r.created_at.isoformat(),
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
    return ExceptionOut(
        id=str(row.id),
        decision_id=str(row.decision_id),
        receipt_id=str(decision.receipt_id),
        simulated=receipt.simulated,
        reason_code=row.reason_code.value,
        evidence=row.evidence,
        status=row.status.value,
        resolved_by=row.resolved_by,
        resolution=row.resolution,
        policy_version=policy.version,
        created_at=row.created_at.isoformat(),
    )
