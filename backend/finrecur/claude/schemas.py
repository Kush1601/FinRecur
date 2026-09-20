"""Pydantic response shapes for the two places Claude is called (spec 3.4 step 4b/5).
Schema validation is the first gate; row validation against the actual data happens
in grouping/explain.py and fixes/propose.py."""

from typing import Literal

from pydantic import BaseModel, Field

FixTypeName = Literal[
    "add_counterparty_alias",
    "repair_reference",
    "mark_duplicate",
    "split_receipt",
    "record_fee_deduction",
    "amend_policy",
    "manual_review",
]


class ExplainResult(BaseModel):
    cause: str = Field(max_length=300)
    member_ids_confirmed: list[str]
    member_ids_dropped: list[str] = []
    evidence_cited: list[str]
    confidence: float = Field(ge=0.0, le=1.0)


class AlternativeFix(BaseModel):
    fix_type: FixTypeName
    params: dict
    summary: str


class ProposeResult(BaseModel):
    fix_type: FixTypeName
    params: dict
    summary: str
    alternative: AlternativeFix | None = None
