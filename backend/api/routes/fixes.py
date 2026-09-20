from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.db import get_db
from api.models import Fix
from api.services.fixes import FixesError, apply_fix, approve, edit_fix, reject, run_dry_run

router = APIRouter(tags=["fixes"])


class FixOut(BaseModel):
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


def _fix_out(fix: Fix) -> FixOut:
    return FixOut(
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


class DryRunOut(BaseModel):
    fix_id: str
    fix_version: int
    predicted: dict
    side_effects: list[dict]
    snapshot_hash: str | None
    stale: bool = False


class EditFixRequest(BaseModel):
    params: dict
    actor_name: str = "Reviewer"


class DecisionRequest(BaseModel):
    role: str
    name: str
    note: str | None = None


class ApprovalOut(BaseModel):
    role: str
    name: str
    decision: str
    note: str | None
    created_at: str


class ApproveOut(BaseModel):
    fix: FixOut
    approvals: list[ApprovalOut]
    needs_second_approval: bool
    stale: bool = False


class InvariantOut(BaseModel):
    name: str
    passed: bool
    detail: str | None


class VerificationOut(BaseModel):
    prediction_matched: bool
    invariants: list[InvariantOut]
    outcome: str


class ApplyOut(BaseModel):
    fix: FixOut
    verification: VerificationOut | None
    stale: bool = False


@router.get("/fixes/{fix_id}", response_model=FixOut)
def get_fix(fix_id: str, db: Session = Depends(get_db)) -> FixOut:
    fix = db.get(Fix, fix_id)
    if fix is None:
        raise HTTPException(status_code=404, detail="fix not found")
    return _fix_out(fix)


@router.post("/fixes/{fix_id}/dry-run", response_model=DryRunOut)
def dry_run_route(fix_id: str, db: Session = Depends(get_db)) -> DryRunOut:
    try:
        outcome = run_dry_run(db, fix_id)
    except FixesError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    if outcome.rejected or outcome.dry_run is None:
        raise HTTPException(status_code=422, detail=outcome.reason or "dry run failed")
    return DryRunOut(
        fix_id=str(outcome.fix.id),
        fix_version=outcome.dry_run.fix_version,
        predicted=outcome.dry_run.predicted_counts,
        side_effects=outcome.dry_run.side_effects,
        snapshot_hash=outcome.fix.snapshot_hash,
        stale=False,
    )


@router.post("/fixes/{fix_id}/edit", response_model=FixOut)
def edit_fix_route(fix_id: str, body: EditFixRequest, db: Session = Depends(get_db)) -> FixOut:
    try:
        new_fix = edit_fix(db, fix_id, body.params, body.actor_name)
    except FixesError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _fix_out(new_fix)


@router.post("/fixes/{fix_id}/approve", response_model=ApproveOut)
def approve_route(fix_id: str, body: DecisionRequest, db: Session = Depends(get_db)) -> ApproveOut:
    try:
        outcome = approve(db, fix_id, body.role, body.name, body.note)
    except FixesError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return ApproveOut(
        fix=_fix_out(outcome.fix),
        approvals=[
            ApprovalOut(
                role=a.role.value,
                name=a.name,
                decision=a.decision.value,
                note=a.note,
                created_at=a.created_at.isoformat(),
            )
            for a in outcome.approvals
        ],
        needs_second_approval=outcome.needs_second_approval,
        stale=outcome.stale,
    )


@router.post("/fixes/{fix_id}/reject", response_model=FixOut)
def reject_route(fix_id: str, body: DecisionRequest, db: Session = Depends(get_db)) -> FixOut:
    try:
        fix = reject(db, fix_id, body.role, body.name, body.note)
    except FixesError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _fix_out(fix)


@router.post("/fixes/{fix_id}/apply", response_model=ApplyOut)
def apply_route(fix_id: str, db: Session = Depends(get_db)) -> ApplyOut:
    try:
        outcome = apply_fix(db, fix_id)
    except FixesError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    verification = None
    if outcome.verification is not None:
        verification = VerificationOut(
            prediction_matched=outcome.verification.prediction_matched,
            invariants=[InvariantOut(**i) for i in outcome.verification.invariants],
            outcome=outcome.verification.outcome.value,
        )
    return ApplyOut(fix=_fix_out(outcome.fix), verification=verification, stale=outcome.stale)
