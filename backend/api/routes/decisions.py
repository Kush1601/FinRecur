from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import get_db
from api.models import Counterparty, Decision, ExceptionRecord, PolicyVersion, Receipt

router = APIRouter(tags=["decisions"])


class DecisionOut(BaseModel):
    id: str
    receipt_id: str
    counterparty_name: str
    amount: int
    simulated: bool
    outcome: str
    rule_id: str | None
    matched_by: str | None
    reason_code: str | None
    evidence: dict
    reviewed_by: str | None
    reviewed_at: str | None
    review_outcome: str | None
    policy_version: int
    created_at: str


class Page(BaseModel):
    items: list[DecisionOut]
    total: int


@router.get("/decisions", response_model=Page)
def list_decisions(
    run_id: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> Page:
    stmt = (
        select(Decision, Receipt, Counterparty, ExceptionRecord, PolicyVersion)
        .join(Receipt, Decision.receipt_id == Receipt.id)
        .join(Counterparty, Receipt.counterparty_id == Counterparty.id)
        .outerjoin(ExceptionRecord, ExceptionRecord.decision_id == Decision.id)
        .join(PolicyVersion, Decision.policy_version_id == PolicyVersion.id)
    )
    if run_id is not None:
        stmt = stmt.where(Decision.run_id == run_id)

    total = len(db.execute(stmt).all())
    rows = db.execute(stmt.order_by(Decision.created_at).limit(limit).offset(offset)).all()
    return Page(
        items=[
            DecisionOut(
                id=str(decision.id),
                receipt_id=str(decision.receipt_id),
                counterparty_name=counterparty.name,
                amount=receipt.amount,
                simulated=receipt.simulated,
                outcome=decision.outcome.value,
                rule_id=decision.rule_id,
                matched_by=decision.matched_by,
                reason_code=exception.reason_code.value if exception else None,
                evidence=decision.evidence,
                reviewed_by=decision.reviewed_by,
                reviewed_at=decision.reviewed_at.isoformat() if decision.reviewed_at else None,
                review_outcome=decision.review_outcome,
                policy_version=policy.version,
                created_at=decision.created_at.isoformat(),
            )
            for decision, receipt, counterparty, exception, policy in rows
        ],
        total=total,
    )
