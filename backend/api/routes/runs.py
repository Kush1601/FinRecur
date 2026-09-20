import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from api.db import get_db
from api.models import (
    Counterparty,
    Decision,
    ExceptionRecord,
    PolicyVersion,
    Receipt,
    Run,
    RunReport,
)
from api.services.runs import RunFailed, execute_run

router = APIRouter(tags=["runs"])


class CreateRunRequest(BaseModel):
    batch: Literal["day1", "day2"]
    policy_version: int | None = None


class RunSummary(BaseModel):
    id: str
    batch_label: str
    policy_version_id: str
    policy_version: int
    counts: dict
    duration_ms: int | None
    created_at: str


def _to_summary(run: Run, db: Session) -> RunSummary:
    policy = db.get(PolicyVersion, run.policy_version_id)
    assert policy is not None
    return RunSummary(
        id=str(run.id),
        batch_label=run.batch_label,
        policy_version_id=str(run.policy_version_id),
        policy_version=policy.version,
        counts=run.counts,
        duration_ms=run.duration_ms,
        created_at=run.created_at.isoformat(),
    )


@router.post("/runs", response_model=RunSummary)
def create_run(body: CreateRunRequest, db: Session = Depends(get_db)) -> RunSummary:
    try:
        run = execute_run(db, body.batch, body.policy_version)
    except RunFailed as e:
        raise HTTPException(status_code=422, detail=f"invariants failed: {e}") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _to_summary(run, db)


@router.get("/runs", response_model=list[RunSummary])
def list_runs(db: Session = Depends(get_db)) -> list[RunSummary]:
    rows = db.scalars(select(Run).order_by(Run.created_at.desc())).all()
    return [_to_summary(r, db) for r in rows]


@router.get("/runs/{run_id}", response_model=RunSummary)
def get_run(run_id: str, db: Session = Depends(get_db)) -> RunSummary:
    row = db.get(Run, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _to_summary(row, db)


@router.get("/runs/{run_id}/report")
def get_run_report(run_id: str, db: Session = Depends(get_db)) -> dict:
    row = db.scalar(select(RunReport).where(RunReport.run_id == run_id))
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")
    return row.report


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.get("/runs/{run_id}/stream")
def stream_run(run_id: str, db: Session = Depends(get_db)) -> StreamingResponse:
    """Replays an already-completed run's decisions as SSE, one `decision` event per
    receipt then `done` with counts. POST /runs already ran everything synchronously and
    wrote every Decision row; this is a read-only replay of that record, not a second
    execution -- there's no in-process worker to attach a live stream to."""
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    rows = db.execute(
        select(Decision, Receipt, Counterparty, ExceptionRecord)
        .join(Receipt, Decision.receipt_id == Receipt.id)
        .join(Counterparty, Receipt.counterparty_id == Counterparty.id)
        .outerjoin(ExceptionRecord, ExceptionRecord.decision_id == Decision.id)
        .where(Decision.run_id == run_id)
        .order_by(Decision.created_at)
    ).all()

    def generate():
        for decision, receipt, counterparty, exception in rows:
            yield _sse(
                "decision",
                {
                    "id": str(decision.id),
                    "counterparty_name": counterparty.name,
                    "amount": receipt.amount,
                    "simulated": receipt.simulated,
                    "outcome": decision.outcome.value,
                    "rule_id": decision.rule_id,
                    "matched_by": decision.matched_by,
                    "reason_code": exception.reason_code.value if exception else None,
                },
            )
        yield _sse("done", {"id": str(run.id), "counts": run.counts})

    return StreamingResponse(generate(), media_type="text/event-stream")
