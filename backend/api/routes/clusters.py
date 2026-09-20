import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import get_db
from api.models import (
    Cluster,
    ClusterStatus,
    Counterparty,
    Fix,
    FixStatus,
    FixType,
    LedgerEvent,
    Receipt,
    Run,
)
from api.services.grouping import (
    explain_all_open_code_clusters,
    explain_and_propose_cluster,
    run_grouping_for_run,
)
from api.settings import settings

router = APIRouter(tags=["clusters"])


class ClusterOut(BaseModel):
    id: str
    run_id: str
    kind: str
    source: str
    status: str
    cause: str | None
    confidence: float | None
    computed_summary: str
    explanation_status: str
    member_count: int
    money_centavos: int
    created_at: str


class ClusterMemberOut(BaseModel):
    receipt_id: str
    counterparty: str | None
    amount_centavos: int
    simulated: bool
    reason_code: str | None
    adjustment_reason: str | None
    matched_by: str | None
    features: dict
    evidence: dict


class ClusterFixOut(BaseModel):
    id: str
    cluster_id: str
    type: str
    version: int
    params: dict
    summary: str | None
    is_widening: bool
    is_alternative: bool
    proposed_by: str
    status: str
    snapshot_hash: str | None


class ClusterDetail(ClusterOut):
    evidence_cited: dict
    member_ids: list[str]
    members: list[ClusterMemberOut]
    fixes: list[ClusterFixOut]


def _fix_out(fix: Fix) -> ClusterFixOut:
    return ClusterFixOut(
        id=str(fix.id),
        cluster_id=str(fix.cluster_id),
        type=fix.type.value,
        version=fix.version,
        params=fix.params,
        summary=fix.summary,
        is_widening=fix.is_widening,
        is_alternative=fix.is_alternative,
        proposed_by=fix.proposed_by,
        status=fix.status.value,
        snapshot_hash=fix.snapshot_hash,
    )


def _detail(db: Session, cluster: Cluster) -> ClusterDetail:
    evidence = cluster.evidence_cited or {}
    features: dict[str, dict] = evidence.get("features", {})
    context: dict[str, dict] = evidence.get("context", {})
    member_ids = list(features)
    receipt_rows = db.scalars(
        select(Receipt).where(Receipt.id.in_([uuid.UUID(receipt_id) for receipt_id in member_ids]))
    ).all()
    receipts = {str(row.id): row for row in receipt_rows}
    counterparty_ids = {row.counterparty_id for row in receipt_rows}
    counterparty_names = {
        str(row.id): row.name
        for row in db.scalars(
            select(Counterparty).where(Counterparty.id.in_(counterparty_ids))
        ).all()
    }
    members = [
        ClusterMemberOut(
            receipt_id=receipt_id,
            counterparty=counterparty_names.get(str(receipts[receipt_id].counterparty_id))
            if receipt_id in receipts
            else feature.get("counterparty_norm"),
            amount_centavos=int(feature.get("receipt_amount", 0)),
            simulated=receipts[receipt_id].simulated if receipt_id in receipts else False,
            reason_code=feature.get("reason_code"),
            adjustment_reason=feature.get("adjustment_reason"),
            matched_by=feature.get("matched_by"),
            features=feature,
            evidence=context.get(receipt_id, {}),
        )
        for receipt_id, feature in features.items()
    ]
    fixes = db.scalars(
        select(Fix).where(Fix.cluster_id == cluster.id).order_by(Fix.is_alternative, Fix.created_at)
    ).all()
    summary = _to_summary(cluster, len(member_ids))
    return ClusterDetail(
        **summary.model_dump(),
        evidence_cited=evidence,
        member_ids=member_ids,
        members=members,
        fixes=[_fix_out(fix) for fix in fixes],
    )


def _money_at_stake(features: dict) -> int:
    # Sum of the member receipts. The summary string carries the same figure for humans;
    # the UI sorts on this one.
    return sum(int(f.get("receipt_amount", 0)) for f in features.values())


def _to_summary(cluster: Cluster, member_count: int) -> ClusterOut:
    evidence = cluster.evidence_cited or {}
    return ClusterOut(
        id=str(cluster.id),
        run_id=str(cluster.run_id),
        kind=cluster.kind.value,
        source=cluster.source.value,
        status=cluster.status.value,
        cause=cluster.cause,
        confidence=cluster.confidence,
        computed_summary=evidence.get("computed_summary", ""),
        explanation_status=evidence.get("explanation_status", "not_yet_explained"),
        member_count=member_count,
        money_centavos=_money_at_stake(evidence.get("features", {})),
        created_at=cluster.created_at.isoformat(),
    )


@router.post("/runs/{run_id}/group", response_model=list[ClusterOut])
def group_run(run_id: str, db: Session = Depends(get_db)) -> list[ClusterOut]:
    if db.get(Run, run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    clusters = run_grouping_for_run(db, run_id)
    return [_to_summary(c, len((c.evidence_cited or {}).get("features", {}))) for c in clusters]


@router.get("/clusters", response_model=list[ClusterOut])
def list_clusters(
    run_id: str | None = None,
    status: ClusterStatus | None = None,
    db: Session = Depends(get_db),
) -> list[ClusterOut]:
    stmt = select(Cluster)
    if run_id is not None:
        stmt = stmt.where(Cluster.run_id == run_id)
    if status is not None:
        stmt = stmt.where(Cluster.status == status)
    rows = db.scalars(stmt.order_by(Cluster.created_at)).all()
    return [_to_summary(c, len((c.evidence_cited or {}).get("features", {}))) for c in rows]


@router.get("/clusters/{cluster_id}", response_model=ClusterDetail)
def get_cluster(cluster_id: str, db: Session = Depends(get_db)) -> ClusterDetail:
    cluster = db.get(Cluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="cluster not found")
    return _detail(db, cluster)


@router.post("/clusters/{cluster_id}/explain", response_model=ClusterDetail)
def explain_cluster_route(cluster_id: str, db: Session = Depends(get_db)) -> ClusterDetail:
    cluster = db.get(Cluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="cluster not found")
    cluster = explain_and_propose_cluster(db, cluster, settings.ANTHROPIC_API_KEY)
    return _detail(db, cluster)


class ManualFixRequest(BaseModel):
    type: FixType
    params: dict
    actor_name: str


@router.post("/clusters/{cluster_id}/fixes", response_model=ClusterFixOut)
def propose_manual_fix(
    cluster_id: str, body: ManualFixRequest, db: Session = Depends(get_db)
) -> ClusterFixOut:
    cluster = db.get(Cluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="cluster not found")
    now = datetime.now(UTC)
    fix = Fix(
        cluster_id=cluster.id,
        type=body.type,
        version=1,
        params=body.params,
        summary=f"Reviewer-proposed {body.type.value.replace('_', ' ')}",
        is_widening=False,
        proposed_by=body.actor_name,
        is_alternative=False,
        status=FixStatus.PROPOSED,
        created_at=now,
    )
    db.add(fix)
    db.flush()
    db.add(
        LedgerEvent(
            actor=body.actor_name,
            action="propose_fix",
            entity_type="fix",
            entity_id=fix.id,
            after={"type": fix.type.value, "params": fix.params, "source": "reviewer"},
            created_at=now,
        )
    )
    db.commit()
    db.refresh(fix)
    return _fix_out(fix)


@router.post("/runs/{run_id}/explain-all", response_model=list[ClusterOut])
async def explain_all_route(run_id: str, db: Session = Depends(get_db)) -> list[ClusterOut]:
    if db.get(Run, run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    clusters = await explain_all_open_code_clusters(db, run_id, settings.ANTHROPIC_API_KEY)
    return [_to_summary(c, len((c.evidence_cited or {}).get("features", {}))) for c in clusters]
