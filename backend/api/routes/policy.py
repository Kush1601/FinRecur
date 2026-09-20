import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import Response

from api.db import get_db
from api.models import PolicyVersion

router = APIRouter(tags=["policy"])


class PolicyVersionOut(BaseModel):
    version: int
    created_at: str
    created_by: str
    fix_id: str | None


class CurrentPolicyOut(BaseModel):
    version: int
    rules: dict


class PolicyOut(BaseModel):
    current: CurrentPolicyOut
    versions: list[PolicyVersionOut]


@router.get("/policy", response_model=PolicyOut)
def get_policy(db: Session = Depends(get_db)) -> PolicyOut:
    rows = db.scalars(select(PolicyVersion).order_by(PolicyVersion.version)).all()
    current = rows[-1]
    return PolicyOut(
        current=CurrentPolicyOut(version=current.version, rules=current.rules),
        versions=[
            PolicyVersionOut(
                version=r.version,
                created_at=r.created_at.isoformat(),
                created_by=r.created_by,
                fix_id=str(r.fix_id) if r.fix_id else None,
            )
            for r in rows
        ],
    )


@router.get("/policy/export")
def export_policy(db: Session = Depends(get_db)) -> Response:
    rows = db.scalars(select(PolicyVersion).order_by(PolicyVersion.version)).all()
    payload = [
        {
            "version": r.version,
            "rules": r.rules,
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat(),
            "fix_id": str(r.fix_id) if r.fix_id else None,
        }
        for r in rows
    ]
    return Response(
        content=json.dumps(payload, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="policy.json"'},
    )
