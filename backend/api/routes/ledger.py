from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import get_db
from api.models import Cluster, Decision, Fix, LedgerEvent

router = APIRouter(tags=["ledger"])


class LedgerEventOut(BaseModel):
    id: str
    actor: str
    action: str
    entity_type: str
    entity_id: str
    before: dict | None
    after: dict | None
    created_at: str


class Page(BaseModel):
    items: list[LedgerEventOut]
    total: int


@router.get("/ledger", response_model=Page)
def list_ledger(
    actor: str | None = None,
    entity_type: str | None = None,
    run_id: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, le=200, ge=1),
    db: Session = Depends(get_db),
) -> Page:
    stmt = select(LedgerEvent)
    if actor is not None:
        stmt = stmt.where(LedgerEvent.actor == actor)
    if entity_type is not None:
        stmt = stmt.where(LedgerEvent.entity_type == entity_type)
    if run_id is not None:
        # entity_id may be a fix/cluster tied to this run; entity_type "fix"/"cluster"
        # events are matched by joining through their owning cluster.
        fix_ids = db.scalars(
            select(Fix.id)
            .join(Cluster, Fix.cluster_id == Cluster.id)
            .where(Cluster.run_id == run_id)
        ).all()
        cluster_ids = db.scalars(select(Cluster.id).where(Cluster.run_id == run_id)).all()
        decision_ids = db.scalars(select(Decision.id).where(Decision.run_id == run_id)).all()
        entity_ids = [*fix_ids, *cluster_ids, *decision_ids]
        stmt = stmt.where(LedgerEvent.entity_id.in_(entity_ids))

    total = len(db.scalars(stmt).all())
    rows = db.scalars(
        stmt.order_by(LedgerEvent.created_at.desc()).limit(page_size).offset((page - 1) * page_size)
    ).all()
    return Page(
        items=[
            LedgerEventOut(
                id=str(r.id),
                actor=r.actor,
                action=r.action,
                entity_type=r.entity_type,
                entity_id=str(r.entity_id),
                before=r.before,
                after=r.after,
                created_at=r.created_at.isoformat(),
            )
            for r in rows
        ],
        total=total,
    )
