import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import get_db
from api.models import ConditionSignature, Fix, PolicyVersion, RecurrenceSample, Run

router = APIRouter(tags=["recurrence"])


class RecurrenceSampleOut(BaseModel):
    run_id: str
    batch_label: str
    policy_version: int
    appeared: int
    auto_handled: int
    needed_human: int


class RecurrenceOut(BaseModel):
    fix_id: str
    fix_type: str
    summary: str | None
    condition: str
    samples: list[RecurrenceSampleOut]


@router.get("/recurrence", response_model=list[RecurrenceOut])
def list_recurrence(db: Session = Depends(get_db)) -> list[RecurrenceOut]:
    signatures = db.scalars(
        select(ConditionSignature).order_by(ConditionSignature.created_at)
    ).all()
    output: list[RecurrenceOut] = []
    for signature in signatures:
        fix = db.get(Fix, signature.fix_id)
        if fix is None:
            continue
        rows = db.scalars(
            select(RecurrenceSample)
            .where(RecurrenceSample.signature_id == signature.id)
            .order_by(RecurrenceSample.created_at)
        ).all()
        samples: list[RecurrenceSampleOut] = []
        for row in rows:
            run = db.get(Run, row.run_id)
            if run is None:
                continue
            policy = db.get(PolicyVersion, run.policy_version_id)
            assert policy is not None
            samples.append(
                RecurrenceSampleOut(
                    run_id=str(run.id),
                    batch_label=run.batch_label,
                    policy_version=policy.version,
                    appeared=row.appeared,
                    auto_handled=row.auto_handled,
                    needed_human=row.needed_human,
                )
            )
        output.append(
            RecurrenceOut(
                fix_id=str(fix.id),
                fix_type=fix.type.value,
                summary=fix.summary,
                condition=json.dumps(signature.predicate, sort_keys=True),
                samples=samples,
            )
        )
    return output
