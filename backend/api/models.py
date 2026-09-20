"""SQLAlchemy 2.0 models for every table in spec 3.6. Money is always BigInteger
centavos -- never Numeric/Float. The ledger (LedgerEvent, Approval, PolicyVersion,
Verification) is append-only; there is no DELETE route for those tables."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


# --- enums -------------------------------------------------------------------


class ReceivableStatus(enum.StrEnum):
    OPEN = "open"
    PARTIALLY_PAID = "partially_paid"
    SETTLED = "settled"
    CANCELLED = "cancelled"


class AdjustmentReason(enum.StrEnum):
    ROUNDING = "rounding"
    SHORT_PAY = "short_pay"
    TOLERANCE = "tolerance"
    FEE = "fee"


class UnappliedCreditStatus(enum.StrEnum):
    HELD = "held"
    REFUNDED = "refunded"
    APPLIED = "applied"


class DecisionOutcome(enum.StrEnum):
    SETTLED = "settled"
    ESCALATED = "escalated"


class ReasonCode(enum.StrEnum):
    NO_MATCH = "no_match"
    MULTIPLE_CANDIDATES = "multiple_candidates"
    SHORT_PAY = "short_pay"
    OVERPAYMENT = "overpayment"
    PARTIAL_PAYMENT_PENDING = "partial_payment_pending"
    PAYMENT_ON_CANCELLED = "payment_on_cancelled"
    DUPLICATE_RECEIPT = "duplicate_receipt"
    SUSPECTED_DUPLICATE = "suspected_duplicate"
    FEE_DEDUCTED = "fee_deducted"
    ONE_RECEIPT_MANY_RECEIVABLES = "one_receipt_many_receivables"


class ExceptionStatus(enum.StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class ClusterKind(enum.StrEnum):
    ESCALATED = "escalated"
    ABSORBED = "absorbed"
    MIXED = "mixed"
    RESCUED = "rescued"


class ClusterSource(enum.StrEnum):
    CODE = "code"
    CLAUDE = "claude"


class ClusterStatus(enum.StrEnum):
    OPEN = "open"
    FIXED = "fixed"
    DISMISSED = "dismissed"


class FixType(enum.StrEnum):
    F1_ADD_COUNTERPARTY_ALIAS = "add_counterparty_alias"
    F2_REPAIR_REFERENCE = "repair_reference"
    F3_MARK_DUPLICATE = "mark_duplicate"
    F4_SPLIT_RECEIPT = "split_receipt"
    F5_RECORD_FEE_DEDUCTION = "record_fee_deduction"
    F6_AMEND_POLICY = "amend_policy"
    F7_MANUAL_REVIEW = "manual_review"


class FixStatus(enum.StrEnum):
    PROPOSED = "proposed"
    DRY_RUN = "dry_run"
    APPROVED = "approved"
    APPLIED = "applied"
    REJECTED = "rejected"
    VERIFICATION_FAILED = "verification_failed"
    REVERTED = "reverted"
    SUPERSEDED = "superseded"


class ApprovalRole(enum.StrEnum):
    REVIEWER = "reviewer"
    APPROVER = "approver"


class ApprovalDecision(enum.StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class VerificationOutcome(enum.StrEnum):
    VERIFIED = "verified"
    VERIFICATION_FAILED = "verification_failed"


# --- source data ---------------------------------------------------------------


class Counterparty(Base):
    __tablename__ = "counterparties"

    id: Mapped[uuid.UUID] = _uuid_pk()
    external_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    aliases: Mapped[list["CounterpartyAlias"]] = relationship(back_populates="canonical")


class CounterpartyAlias(Base):
    __tablename__ = "counterparty_aliases"

    id: Mapped[uuid.UUID] = _uuid_pk()
    canonical_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("counterparties.id"))
    alias_external_id: Mapped[str] = mapped_column(String, index=True)
    alias_name: Mapped[str] = mapped_column(String)
    fix_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("fixes.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    canonical: Mapped["Counterparty"] = relationship(back_populates="aliases")


class Receivable(Base):
    __tablename__ = "receivables"

    id: Mapped[uuid.UUID] = _uuid_pk()
    counterparty_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("counterparties.id"), index=True)
    total: Mapped[int] = mapped_column(BigInteger)
    shipping: Mapped[int] = mapped_column(BigInteger, default=0)
    allocated: Mapped[int] = mapped_column(BigInteger, default=0)
    adjusted: Mapped[int] = mapped_column(BigInteger, default=0)
    remaining: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[ReceivableStatus] = mapped_column(
        Enum(ReceivableStatus, name="receivable_status")
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String, default="olist")
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    counterparty_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("counterparties.id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reference: Mapped[str] = mapped_column(String, default="", index=True)
    original_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_of: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("receipts.id"), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)


# --- settlement -----------------------------------------------------------------


class Allocation(Base):
    __tablename__ = "allocations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    receipt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receipts.id"))
    receivable_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receivables.id"))
    amount: Mapped[int] = mapped_column(BigInteger)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("decisions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Adjustment(Base):
    __tablename__ = "adjustments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    receivable_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receivables.id"))
    amount: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[AdjustmentReason] = mapped_column(
        Enum(AdjustmentReason, name="adjustment_reason")
    )
    decision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("decisions.id"), nullable=True)
    fix_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("fixes.id"), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UnappliedCredit(Base):
    __tablename__ = "unapplied_credits"

    id: Mapped[uuid.UUID] = _uuid_pk()
    counterparty_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("counterparties.id"))
    receipt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receipts.id"))
    amount: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[UnappliedCreditStatus] = mapped_column(
        Enum(UnappliedCreditStatus, name="unapplied_credit_status")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --- bot work --------------------------------------------------------------------


class PolicyVersion(Base):
    __tablename__ = "policy_versions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    version: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    rules: Mapped[dict] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("policy_versions.id"), nullable=True
    )
    # policy_versions -> fixes -> clusters -> runs -> policy_versions would otherwise be a
    # circular FK dependency; defer this one so table creation order can break the cycle.
    fix_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("fixes.id", use_alter=True, name="fk_policy_versions_fix_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    policy_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policy_versions.id"))
    batch_label: Mapped[str] = mapped_column(String)
    counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), index=True)
    receipt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receipts.id"))
    outcome: Mapped[DecisionOutcome] = mapped_column(Enum(DecisionOutcome, name="decision_outcome"))
    rule_id: Mapped[str | None] = mapped_column(String, nullable=True)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policy_versions.id"))
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    matched_by: Mapped[str | None] = mapped_column(String, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExceptionRecord(Base):
    __tablename__ = "exceptions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    decision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("decisions.id"))
    reason_code: Mapped[ReasonCode] = mapped_column(Enum(ReasonCode, name="reason_code"))
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[ExceptionStatus] = mapped_column(
        Enum(ExceptionStatus, name="exception_status"), index=True
    )
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --- clusters and fixes -------------------------------------------------------------


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"))
    cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    evidence_cited: Mapped[dict] = mapped_column(JSONB, default=dict)
    kind: Mapped[ClusterKind] = mapped_column(Enum(ClusterKind, name="cluster_kind"))
    source: Mapped[ClusterSource] = mapped_column(Enum(ClusterSource, name="cluster_source"))
    status: Mapped[ClusterStatus] = mapped_column(Enum(ClusterStatus, name="cluster_status"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ClusterMember(Base):
    __tablename__ = "cluster_members"

    id: Mapped[uuid.UUID] = _uuid_pk()
    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clusters.id"), index=True)
    exception_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("exceptions.id"), nullable=True
    )
    adjustment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("adjustments.id"), nullable=True
    )
    # A "rescued" member (spec 3.4 milestone-1 facts: fault B's R2 rescues) is neither
    # an open exception nor an auto-applied write-off -- it's a Decision that matched,
    # so it needs its own nullable FK rather than overloading the other two.
    decision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("decisions.id"), nullable=True)


class Fix(Base):
    __tablename__ = "fixes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clusters.id"))
    type: Mapped[FixType] = mapped_column(Enum(FixType, name="fix_type"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_widening: Mapped[bool] = mapped_column(Boolean, default=False)
    # "agent" for a Claude-authored proposal (step 5), "reviewer" for a code-only
    # cluster's manually-picked fix (spec 3.7: "Grouped by code" path).
    proposed_by: Mapped[str] = mapped_column(String, default="agent")
    is_alternative: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[FixStatus] = mapped_column(Enum(FixStatus, name="fix_status"))
    inverse: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    snapshot_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DryRun(Base):
    __tablename__ = "dry_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    fix_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fixes.id"))
    fix_version: Mapped[int] = mapped_column(Integer)
    predicted_counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    side_effects: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = _uuid_pk()
    fix_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fixes.id"))
    fix_version: Mapped[int] = mapped_column(Integer)
    role: Mapped[ApprovalRole] = mapped_column(Enum(ApprovalRole, name="approval_role"))
    name: Mapped[str] = mapped_column(String)
    decision: Mapped[ApprovalDecision] = mapped_column(
        Enum(ApprovalDecision, name="approval_decision")
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Verification(Base):
    __tablename__ = "verifications"

    id: Mapped[uuid.UUID] = _uuid_pk()
    fix_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fixes.id"))
    prediction_matched: Mapped[bool] = mapped_column(Boolean)
    invariants: Mapped[dict] = mapped_column(JSONB, default=dict)
    outcome: Mapped[VerificationOutcome] = mapped_column(
        Enum(VerificationOutcome, name="verification_outcome")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --- recurrence -----------------------------------------------------------------


class ConditionSignature(Base):
    __tablename__ = "condition_signatures"

    id: Mapped[uuid.UUID] = _uuid_pk()
    fix_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fixes.id"))
    predicate: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RecurrenceSample(Base):
    __tablename__ = "recurrence_samples"

    id: Mapped[uuid.UUID] = _uuid_pk()
    signature_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("condition_signatures.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"))
    appeared: Mapped[int] = mapped_column(Integer, default=0)
    auto_handled: Mapped[int] = mapped_column(Integer, default=0)
    needed_human: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --- audit ------------------------------------------------------------------------


class LedgerEvent(Base):
    __tablename__ = "ledger_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    actor: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    entity_type: Mapped[str] = mapped_column(String)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    before: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (Index("ix_ledger_events_entity", "entity_type", "entity_id"),)


class RunReport(Base):
    __tablename__ = "run_reports"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), unique=True)
    report: Mapped[dict] = mapped_column(JSONB)
    report_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
