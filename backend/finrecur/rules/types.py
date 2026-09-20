"""Shapes the rule engine returns. No SQLAlchemy, no policy-specific literals here --
this module only names the vocabulary (spec 3.2 Decision + R8 evidence)."""

import enum
from dataclasses import dataclass, field
from typing import Literal


class ReasonCode(enum.StrEnum):
    NO_MATCH = "no_match"
    MULTIPLE_CANDIDATES = "multiple_candidates"
    SHORT_PAY = "short_pay"
    OVERPAYMENT = "overpayment"
    PARTIAL_PAYMENT_PENDING = "partial_payment_pending"
    PAYMENT_ON_CANCELLED = "payment_on_cancelled"
    SUSPECTED_DUPLICATE = "suspected_duplicate"
    FEE_DEDUCTED = "fee_deducted"
    ONE_RECEIPT_MANY_RECEIVABLES = "one_receipt_many_receivables"


Outcome = Literal["matched", "escalated"]
MatchedBy = Literal["exact", "group", "amount_date"] | None

# (receivable_id, amount_centavos)
Allocation = tuple[str, int]
# (receivable_id, amount_centavos, reason) -- reason is rounding | short_pay | tolerance
Adjustment = tuple[str, int, str]


@dataclass(frozen=True)
class Decision:
    receipt_id: str
    outcome: Outcome
    rule_id: str
    matched_by: MatchedBy
    allocations: list[Allocation] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)
    unapplied_credit: int = 0
    reason_code: ReasonCode | None = None
    evidence: dict = field(default_factory=dict)
