"""Dry run, edit, approve, reject, apply, verify (spec 3.4 steps 9-10). The pure
fix engine (finrecur.fixes.engine / finrecur.dryrun) never touches the DB; this
module loads rows, calls it, and writes results inside transactions. Every write
gets a LedgerEvent -- the ledger is append-only, there is no DELETE route for it."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from api.models import Adjustment as AdjustmentModel
from api.models import (
    AdjustmentReason,
    Approval,
    ApprovalDecision,
    ApprovalRole,
    Cluster,
    CounterpartyAlias,
    DecisionOutcome,
    ExceptionStatus,
    Fix,
    FixStatus,
    FixType,
    LedgerEvent,
    ReceivableStatus,
    UnappliedCreditStatus,
    VerificationOutcome,
)
from api.models import Allocation as AllocationModel
from api.models import ConditionSignature as ConditionSignatureModel
from api.models import Counterparty as CounterpartyModel
from api.models import Decision as DecisionModel
from api.models import DryRun as DryRunModel
from api.models import ExceptionRecord as ExceptionModel
from api.models import PolicyVersion as PolicyVersionModel
from api.models import ReasonCode as DbReasonCode
from api.models import Receipt as ReceiptModel
from api.models import Receivable as ReceivableModel
from api.models import Run as RunModel
from api.models import UnappliedCredit as UnappliedCreditModel
from api.models import Verification as VerificationModel
from api.services.ledger_state import current_ledger_state
from api.services.runs import BATCH_LINEAGE, build_aliases
from finrecur.dryrun import DryRunResult
from finrecur.dryrun import decide as decide_all
from finrecur.dryrun import dry_run as pure_dry_run
from finrecur.fixes import engine
from finrecur.fixes.bounds import check_bounds
from finrecur.fixes.widening import classify
from finrecur.invariants import check as check_invariants
from finrecur.result import Err
from finrecur.rules.types import Decision as PureDecision
from finrecur.snapshot import snapshot_hash as compute_snapshot_hash

RERUN_FIX_TYPES = {"add_counterparty_alias", "repair_reference", "mark_duplicate", "amend_policy"}


class FixesError(Exception):
    """Raised for a caller error (fix not found, wrong status) -- routes turn this
    into a 4xx. Distinct from a stale/rejected outcome, which is a normal result
    the caller is meant to handle, not an exception."""


def _ledger(
    session: Session,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    session.add(
        LedgerEvent(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
            created_at=datetime.now(UTC),
        )
    )


def _load_state(session: Session, run: RunModel) -> engine.State:
    batches_included = list(BATCH_LINEAGE[run.batch_label])
    receivable_rows = session.scalars(
        select(ReceivableModel).where(ReceivableModel.meta["batch"].astext.in_(batches_included))
    ).all()
    receipt_rows = session.scalars(
        select(ReceiptModel).where(ReceiptModel.meta["batch"].astext.in_(batches_included))
    ).all()
    pv = session.get(PolicyVersionModel, run.policy_version_id)
    assert pv is not None

    receivables = {
        str(r.id): engine.StateReceivable(
            id=str(r.id),
            counterparty_id=str(r.counterparty_id),
            total=r.total,
            shipping=r.shipping,
            allocated=r.allocated,
            adjusted=r.adjusted,
            remaining=r.remaining,
            status=r.status.value,
            issued_at=r.issued_at,
            meta=r.meta,
        )
        for r in receivable_rows
    }
    receipts = {
        str(r.id): engine.StateReceipt(
            id=str(r.id),
            counterparty_id=str(r.counterparty_id),
            amount=r.amount,
            received_at=r.received_at,
            reference=r.reference,
            original_reference=r.original_reference,
            duplicate_of=str(r.duplicate_of) if r.duplicate_of else None,
            meta=r.meta,
        )
        for r in receipt_rows
    }
    receivable_ids = list(receivables.keys())
    allocations = []
    adjustments = []
    if receivable_ids:
        alloc_rows = session.scalars(
            select(AllocationModel).where(AllocationModel.receivable_id.in_(receivable_ids))
        ).all()
        allocations = [
            engine.StateAllocation(
                receipt_id=str(a.receipt_id), receivable_id=str(a.receivable_id), amount=a.amount
            )
            for a in alloc_rows
        ]
        adj_rows = session.execute(
            select(AdjustmentModel, DecisionModel.receipt_id)
            .outerjoin(DecisionModel, AdjustmentModel.decision_id == DecisionModel.id)
            .where(AdjustmentModel.receivable_id.in_(receivable_ids))
        ).all()
        adjustments = [
            engine.StateAdjustment(
                receivable_id=str(adj.receivable_id),
                amount=adj.amount,
                reason=adj.reason.value,
                receipt_id=str(receipt_id) if receipt_id else None,
            )
            for adj, receipt_id in adj_rows
        ]

    return engine.State(
        receivables=receivables,
        receipts=receipts,
        allocations=allocations,
        adjustments=adjustments,
        aliases=build_aliases(session),
        policy=pv.rules,
    )


def _rows_from_state(state: engine.State, hint: dict[str, str] | None = None) -> engine.Rows:
    receipt_receivable = dict(hint or {})
    for a in state.allocations:
        receipt_receivable.setdefault(a.receipt_id, a.receivable_id)
    return engine.Rows(
        receipt_ids=frozenset(state.receipts),
        receivable_ids=frozenset(state.receivables),
        receipt_amounts={rid: r.amount for rid, r in state.receipts.items()},
        policy=state.policy,
        receipt_receivable=receipt_receivable,
        receivable_totals={rid: r.total for rid, r in state.receivables.items()},
    )


def _affected_receipt_ids(cluster: Cluster) -> list[str]:
    return list((cluster.evidence_cited or {}).get("features", {}).keys())


def _receipt_receivable_hint(cluster: Cluster) -> dict[str, str]:
    context = (cluster.evidence_cited or {}).get("context", {})
    return {rid: ctx["receivable_id"] for rid, ctx in context.items() if ctx.get("receivable_id")}


def _affected_receivable_ids(cluster: Cluster, fix: Fix) -> set[str] | None:
    """None is the sentinel for "every receivable of the run's batch" -- spec 3.4
    step 9: amend_policy dry-runs over all history, so its snapshot and side-effect
    scan cover the whole batch, not just the cluster's own receivables."""
    if fix.type == FixType.F6_AMEND_POLICY:
        return None
    ids: set[str] = set()
    context = (cluster.evidence_cited or {}).get("context", {})
    for ctx in context.values():
        if ctx.get("receivable_id"):
            ids.add(ctx["receivable_id"])
        for rv in ctx.get("candidate_receivable_set") or []:
            ids.add(rv["receivable_id"])
        for rv in ctx.get("counterparty_open_receivables") or []:
            ids.add(rv["id"])
    if fix.type == FixType.F4_SPLIT_RECEIPT:
        for s in fix.params.get("splits", []):
            for a in s.get("allocations", []):
                ids.add(a["receivable_id"])
    return ids


def _snapshot_input(
    state: engine.State, receivable_ids: set[str] | None, receipt_ids: set[str] | None = None
) -> dict:
    ids = receivable_ids if receivable_ids is not None else set(state.receivables)
    receivables = {
        rid: {
            "allocated": rv.allocated,
            "adjusted": rv.adjusted,
            "remaining": rv.remaining,
            "status": rv.status,
        }
        for rid, rv in state.receivables.items()
        if rid in ids
    }
    allocations = sorted(
        (
            {"receipt_id": a.receipt_id, "receivable_id": a.receivable_id, "amount": a.amount}
            for a in state.allocations
            if a.receivable_id in ids
        ),
        key=lambda x: (x["receipt_id"], x["receivable_id"]),
    )
    aliases = sorted(
        ({"alias": k, "canonical": v} for k, v in state.aliases.items()), key=lambda x: x["alias"]
    )
    # A fix's params reference specific receipts by amount, reference, and
    # duplicate marker (F2/F3/F4/F5 all key off these) -- a snapshot that only
    # covered receivable balances could match while the receipt underneath a
    # proposal had already changed (e.g. another fix repaired its reference).
    rcpt_ids = receipt_ids if receipt_ids is not None else set(state.receipts)
    receipts = {
        rid: {
            "amount": r.amount,
            "counterparty_id": r.counterparty_id,
            "reference": r.reference,
            "duplicate_of": r.duplicate_of,
        }
        for rid, r in state.receipts.items()
        if rid in rcpt_ids
    }
    return {
        "receivables": receivables,
        "allocations": allocations,
        "aliases": aliases,
        "receipts": receipts,
    }


def _affected_receipt_scope(cluster: Cluster, fix: Fix, state: engine.State) -> set[str] | None:
    """None means "every receipt in the run's batch" -- amend_policy dry-runs and
    snapshots over all history, so its receipts must be too."""
    if fix.type == FixType.F6_AMEND_POLICY:
        return None
    ids = set(_affected_receipt_ids(cluster))
    for rid in fix.params.get("receipt_ids", []):
        ids.add(rid)
    for pair in fix.params.get("pairs", []):
        ids.add(pair.get("receipt_id"))
        ids.add(pair.get("duplicate_of"))
    for repair in fix.params.get("repairs", []):
        ids.add(repair.get("receipt_id"))
    for split in fix.params.get("splits", []):
        ids.add(split.get("receipt_id"))
    ids.discard(None)
    return {rid for rid in ids if rid in state.receipts}


def _snapshot_for_fix(session: Session, cluster: Cluster, fix: Fix) -> tuple[str, engine.State]:
    run = session.get(RunModel, cluster.run_id)
    assert run is not None
    state = _load_state(session, run)
    receivable_ids = _affected_receivable_ids(cluster, fix)
    receipt_ids = _affected_receipt_scope(cluster, fix, state)
    snapshot = _snapshot_input(state, receivable_ids, receipt_ids)
    return compute_snapshot_hash(snapshot, state.policy), state


def _condition_signature(fix: Fix) -> dict | None:
    """Predicate the fix removes (spec 3.6). Step 11 evaluates these against
    incoming receipts; this module only persists them."""
    p = fix.params
    if fix.type == FixType.F1_ADD_COUNTERPARTY_ALIAS:
        return {"counterparty_id": p["alias_counterparty_id"]}
    if fix.type == FixType.F2_REPAIR_REFERENCE:
        return {"reference_edit_distance": [1, 2]}
    if fix.type == FixType.F3_MARK_DUPLICATE:
        return {"fingerprint_kind": "amount_date_reference"}
    if fix.type == FixType.F4_SPLIT_RECEIPT:
        return {"overpayment_multi_receivable": True}
    if fix.type == FixType.F5_RECORD_FEE_DEDUCTION:
        if p.get("fee_percent") is not None:
            return {
                "payment_type": p.get("payment_type"),
                "shortfall_pct": p["fee_percent"],
                "tol": 0.01,
            }
        # A fixed-centavo fee has no percent to compare -- store the amount
        # instead. (Storing shortfall_pct=None here used to crash the next
        # run's recurrence check with float(None).)
        return {
            "payment_type": p.get("payment_type"),
            "fee_fixed_centavos": p.get("fee_fixed_centavos"),
            "tol_centavos": 1,
        }
    if fix.type == FixType.F6_AMEND_POLICY:
        return {
            "rule_id": p["rule_id"],
            "key": p["key"],
            "before": p["before"],
            "after": p["after"],
        }
    return None


# --- dry run -----------------------------------------------------------------------


@dataclass(frozen=True)
class DryRunOutcome:
    fix: Fix
    dry_run: DryRunModel | None
    rejected: bool = False
    reason: str | None = None


def run_dry_run(session: Session, fix_id: str) -> DryRunOutcome:
    # A dry run is a state transition, and clients can legitimately retry it
    # (React Strict Mode does this during development). Lock the fix row so two
    # concurrent requests cannot create duplicate previews or audit events.
    fix = session.scalar(select(Fix).where(Fix.id == fix_id).with_for_update())
    if fix is None:
        raise FixesError(f"fix {fix_id} not found")
    # A read/refresh of an already-approved fix must never revoke its approval.
    # Return the immutable preview that was approved instead of creating another
    # one and moving the state machine backwards to DRY_RUN.
    if fix.status in {FixStatus.APPROVED, FixStatus.APPLIED}:
        latest = session.scalars(
            select(DryRunModel)
            .where(DryRunModel.fix_id == fix.id, DryRunModel.fix_version == fix.version)
            .order_by(DryRunModel.created_at.desc())
        ).first()
        if latest is None:
            raise FixesError(f"fix {fix_id} has no approved dry run")
        return DryRunOutcome(fix=fix, dry_run=latest)
    if fix.status in {FixStatus.REJECTED, FixStatus.SUPERSEDED, FixStatus.VERIFICATION_FAILED}:
        # A rejected or superseded proposal is a closed chapter -- viewing the
        # cluster (which triggers a dry-run refresh) must not silently revive
        # it into a fresh DRY_RUN, only the edit_fix path is allowed to create
        # a new proposal to take its place.
        raise FixesError(f"fix {fix_id} is {fix.status.value} and cannot be previewed again")
    cluster = session.get(Cluster, fix.cluster_id)
    assert cluster is not None
    run = session.get(RunModel, cluster.run_id)
    assert run is not None
    state = _load_state(session, run)
    now = datetime.now(UTC)

    # Reuse an unchanged preview. This keeps the append-only ledger meaningful:
    # identical refreshes are reads, while changed balances still produce a new
    # dry run and snapshot for approval.
    if fix.status == FixStatus.DRY_RUN:
        current_hash = compute_snapshot_hash(
            _snapshot_input(
                state,
                _affected_receivable_ids(cluster, fix),
                _affected_receipt_scope(cluster, fix, state),
            ),
            state.policy,
        )
        if current_hash == fix.snapshot_hash:
            latest = session.scalars(
                select(DryRunModel)
                .where(DryRunModel.fix_id == fix.id, DryRunModel.fix_version == fix.version)
                .order_by(DryRunModel.created_at.desc())
            ).first()
            if latest is not None:
                return DryRunOutcome(fix=fix, dry_run=latest)

    hint = _receipt_receivable_hint(cluster)
    validation = engine.validate(fix.type.value, fix.params, _rows_from_state(state, hint))
    if isinstance(validation, Err):
        _ledger(
            session, "system", "dry_run_failed", "fix", fix.id, after={"reason": validation.reason}
        )
        session.commit()
        return DryRunOutcome(fix=fix, dry_run=None, rejected=True, reason=validation.reason)

    cluster_size = len((cluster.evidence_cited or {}).get("features", {})) or 1
    is_widening = False
    if fix.type == FixType.F6_AMEND_POLICY:
        p = fix.params
        classification = classify(p["rule_id"], p["key"], p["before"], p["after"])
        is_widening = classification in ("widening", "unsupported")
        bounds = check_bounds(p["key"], p["before"], p["after"], is_widening, cluster_size)
        if not bounds.within_bounds:
            fix.status = FixStatus.REJECTED
            _ledger(
                session,
                "system",
                "reject",
                "fix",
                fix.id,
                after={"reason": "out_of_bounds", "detail": bounds.reason},
            )
            session.commit()
            return DryRunOutcome(
                fix=fix, dry_run=None, rejected=True, reason=f"out_of_bounds: {bounds.reason}"
            )

    affected_receipt_ids = _affected_receipt_ids(cluster)
    result: DryRunResult = pure_dry_run(
        fix.type.value, fix.params, state, affected_receipt_ids, hint
    )

    affected_receivable_ids = _affected_receivable_ids(cluster, fix)
    affected_receipt_scope = _affected_receipt_scope(cluster, fix, state)
    snapshot_input = _snapshot_input(state, affected_receivable_ids, affected_receipt_scope)
    hash_value = compute_snapshot_hash(snapshot_input, state.policy)

    dry_run_row = DryRunModel(
        fix_id=fix.id,
        fix_version=fix.version,
        predicted_counts={
            "matched": result.predicted.matched,
            "still_failing": result.predicted.still_failing,
            "applied_centavos": result.predicted.applied_centavos,
            "written_off_centavos": result.predicted.written_off_centavos,
        },
        side_effects=[
            {
                "receipt_id": e.receipt_id,
                "counterparty_id": e.counterparty_id,
                "amount_centavos": e.amount_centavos,
                "would_become": e.would_become,
                "write_off_centavos": e.write_off_centavos,
            }
            for e in result.side_effects
        ],
        created_at=now,
    )
    session.add(dry_run_row)
    fix.status = FixStatus.DRY_RUN
    fix.snapshot_hash = hash_value
    fix.is_widening = is_widening
    session.flush()
    _ledger(
        session,
        "system",
        "dry_run",
        "fix",
        fix.id,
        after={
            "predicted": dry_run_row.predicted_counts,
            "side_effect_count": len(dry_run_row.side_effects),
            "snapshot_hash": hash_value,
        },
    )
    session.commit()
    session.refresh(fix)
    session.refresh(dry_run_row)
    return DryRunOutcome(fix=fix, dry_run=dry_run_row)


def edit_fix(session: Session, fix_id: str, params: dict, actor_name: str) -> Fix:
    old = session.get(Fix, fix_id)
    if old is None:
        raise FixesError(f"fix {fix_id} not found")
    now = datetime.now(UTC)
    new_fix = Fix(
        cluster_id=old.cluster_id,
        type=old.type,
        version=old.version + 1,
        params=params,
        summary=old.summary,
        is_widening=False,
        proposed_by="reviewer",
        is_alternative=old.is_alternative,
        status=FixStatus.PROPOSED,
        created_at=now,
    )
    session.add(new_fix)
    old.status = FixStatus.SUPERSEDED
    session.flush()
    _ledger(
        session,
        actor_name,
        "edit_fix",
        "fix",
        new_fix.id,
        before={"previous_fix_id": str(old.id), "params": old.params},
        after={"params": new_fix.params},
    )
    session.commit()
    run_dry_run(session, str(new_fix.id))
    session.refresh(new_fix)
    return new_fix


# --- approve / reject ----------------------------------------------------------------


@dataclass(frozen=True)
class ApproveOutcome:
    fix: Fix
    approvals: list[Approval]
    needs_second_approval: bool
    stale: bool = False


def approve(
    session: Session, fix_id: str, role: str, name: str, note: str | None
) -> ApproveOutcome:
    fix = session.get(Fix, fix_id)
    if fix is None:
        raise FixesError(f"fix {fix_id} not found")
    if fix.status != FixStatus.DRY_RUN:
        raise FixesError(f"fix must have a fresh dry run to approve, is {fix.status.value}")

    cluster = session.get(Cluster, fix.cluster_id)
    assert cluster is not None
    current_hash, _state = _snapshot_for_fix(session, cluster, fix)
    existing_approvals = list(
        session.scalars(
            select(Approval).where(Approval.fix_id == fix.id, Approval.fix_version == fix.version)
        ).all()
    )

    if current_hash != fix.snapshot_hash:
        _ledger(
            session,
            name,
            "approve_stale",
            "fix",
            fix.id,
            after={"expected": fix.snapshot_hash, "actual": current_hash},
        )
        session.commit()
        return ApproveOutcome(
            fix=fix, approvals=existing_approvals, needs_second_approval=fix.is_widening, stale=True
        )

    approval = Approval(
        fix_id=fix.id,
        fix_version=fix.version,
        role=ApprovalRole(role),
        name=name,
        decision=ApprovalDecision.APPROVE,
        note=note,
        snapshot_hash=current_hash,
        created_at=datetime.now(UTC),
    )
    session.add(approval)
    session.flush()
    _ledger(
        session, name, "approve", "fix", fix.id, after={"role": role, "name": name, "note": note}
    )
    existing_approvals.append(approval)

    # Only signatures bound to the CURRENT preview count. A dry run reused while
    # a fix sits half-approved (fix.status stays DRY_RUN until the second
    # signature lands) can move fix.snapshot_hash forward; an earlier approver's
    # signature on the stale hash must not be combined with a later one on the
    # new hash to satisfy the two-person rule.
    approvals = [
        a
        for a in existing_approvals
        if a.decision == ApprovalDecision.APPROVE and a.snapshot_hash == fix.snapshot_hash
    ]
    needs_second = False
    if fix.is_widening:
        distinct_names = {a.name for a in approvals}
        distinct_roles = {a.role.value for a in approvals}
        satisfied = (
            len(approvals) >= 2 and len(distinct_names) >= 2 and "approver" in distinct_roles
        )
        if satisfied:
            fix.status = FixStatus.APPROVED
        else:
            needs_second = True
    else:
        fix.status = FixStatus.APPROVED

    session.commit()
    session.refresh(fix)
    return ApproveOutcome(fix=fix, approvals=existing_approvals, needs_second_approval=needs_second)


def reject(session: Session, fix_id: str, role: str, name: str, note: str | None) -> Fix:
    fix = session.get(Fix, fix_id)
    if fix is None:
        raise FixesError(f"fix {fix_id} not found")
    approval = Approval(
        fix_id=fix.id,
        fix_version=fix.version,
        role=ApprovalRole(role),
        name=name,
        decision=ApprovalDecision.REJECT,
        note=note,
        snapshot_hash=fix.snapshot_hash or "",
        created_at=datetime.now(UTC),
    )
    session.add(approval)
    fix.status = FixStatus.REJECTED
    session.flush()
    _ledger(
        session, name, "reject", "fix", fix.id, after={"role": role, "name": name, "note": note}
    )
    session.commit()
    session.refresh(fix)
    return fix


# --- apply -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyOutcome:
    fix: Fix
    verification: VerificationModel | None
    stale: bool = False


def _recompute_status(rv: ReceivableModel) -> None:
    rv.remaining = rv.total - rv.allocated - rv.adjusted
    if rv.status == ReceivableStatus.CANCELLED:
        return
    if rv.remaining <= 0:
        rv.status = ReceivableStatus.SETTLED
    elif rv.allocated > 0 or rv.adjusted > 0:
        rv.status = ReceivableStatus.PARTIALLY_PAID
    else:
        rv.status = ReceivableStatus.OPEN


def _rewrite_decision(
    session: Session,
    run_id,
    receipt_id: str,
    new_decision: PureDecision,
    fix: Fix,
    now: datetime,
    receivable_by_id: dict[str, ReceivableModel],
) -> None:
    """A receipt's outcome changed because of this fix -- write the new
    Allocation/Adjustment rows, update receivable balances, resolve (or reopen)
    its exception, and update the Decision row. `reviewed_by` is set here because
    applying an approved fix IS the explicit review action for these receipts."""
    decision_row = session.scalars(
        select(DecisionModel).where(
            DecisionModel.run_id == run_id, DecisionModel.receipt_id == uuid.UUID(receipt_id)
        )
    ).first()
    if decision_row is None:
        return

    was_escalated = decision_row.outcome == DecisionOutcome.ESCALATED

    # The old decision may already have settled a different way (e.g. R2
    # amount/date before a reference repair lets R1 match it) -- reverse
    # whatever it already wrote before recording the new outcome, or the two
    # sets of allocations/adjustments double up on the same receivable.
    old_allocs = session.scalars(
        select(AllocationModel).where(AllocationModel.decision_id == decision_row.id)
    ).all()
    old_adjs = session.scalars(
        select(AdjustmentModel).where(AdjustmentModel.decision_id == decision_row.id)
    ).all()
    for a in old_allocs:
        receivable_by_id[str(a.receivable_id)].allocated -= a.amount
        session.delete(a)
    for a in old_adjs:
        receivable_by_id[str(a.receivable_id)].adjusted -= a.amount
        session.delete(a)
    if old_allocs or old_adjs:
        session.flush()
        for a in old_allocs:
            _recompute_status(receivable_by_id[str(a.receivable_id)])
        for a in old_adjs:
            _recompute_status(receivable_by_id[str(a.receivable_id)])

    if new_decision.outcome == "matched":
        for rv_id, amount in new_decision.allocations:
            rv_row = receivable_by_id[rv_id]
            alloc_id = uuid.uuid4()
            session.add(
                AllocationModel(
                    id=alloc_id,
                    receipt_id=decision_row.receipt_id,
                    receivable_id=uuid.UUID(rv_id),
                    amount=amount,
                    decision_id=decision_row.id,
                    created_at=now,
                )
            )
            rv_row.allocated += amount
            _ledger(
                session,
                "system",
                "allocate",
                "allocation",
                alloc_id,
                after={
                    "receipt_id": receipt_id,
                    "receivable_id": rv_id,
                    "amount": amount,
                    "fix_id": str(fix.id),
                },
            )
        for rv_id, amount, reason in new_decision.adjustments:
            rv_row = receivable_by_id[rv_id]
            adj_id = uuid.uuid4()
            session.add(
                AdjustmentModel(
                    id=adj_id,
                    receivable_id=uuid.UUID(rv_id),
                    amount=amount,
                    reason=AdjustmentReason(reason),
                    decision_id=decision_row.id,
                    fix_id=fix.id,
                    created_at=now,
                )
            )
            rv_row.adjusted += amount
            _ledger(
                session,
                "system",
                "adjust",
                "adjustment",
                adj_id,
                after={
                    "receivable_id": rv_id,
                    "amount": amount,
                    "reason": reason,
                    "fix_id": str(fix.id),
                },
            )
        for rv_id, _amount in new_decision.allocations:
            _recompute_status(receivable_by_id[rv_id])
        for rv_id, _amount, _reason in new_decision.adjustments:
            _recompute_status(receivable_by_id[rv_id])

        decision_row.outcome = DecisionOutcome.SETTLED
        decision_row.rule_id = new_decision.rule_id
        decision_row.matched_by = new_decision.matched_by
        decision_row.reviewed_by = f"fix:{fix.id}"
        decision_row.reviewed_at = now
        decision_row.review_outcome = "resolved_by_fix"

        if was_escalated:
            exception_row = session.scalars(
                select(ExceptionModel).where(
                    ExceptionModel.decision_id == decision_row.id,
                    ExceptionModel.status == ExceptionStatus.OPEN,
                )
            ).first()
            if exception_row is not None:
                exception_row.status = ExceptionStatus.RESOLVED
                exception_row.resolved_by = f"fix:{fix.id}"
                exception_row.resolution = fix.summary or fix.type.value
    else:
        # A narrowing amendment: this receipt used to settle and no longer does.
        # The old allocations/adjustments were already reversed above.
        decision_row.outcome = DecisionOutcome.ESCALATED
        decision_row.rule_id = new_decision.rule_id
        decision_row.matched_by = None
        decision_row.evidence = new_decision.evidence
        decision_row.reviewed_by = f"fix:{fix.id}"
        decision_row.reviewed_at = now
        decision_row.review_outcome = "reopened_by_fix"
        assert new_decision.reason_code is not None
        session.add(
            ExceptionModel(
                decision_id=decision_row.id,
                reason_code=DbReasonCode(new_decision.reason_code.value),
                evidence=new_decision.evidence,
                status=ExceptionStatus.OPEN,
                created_at=now,
            )
        )


def _apply_data_fix(
    session: Session,
    fix: Fix,
    cluster: Cluster,
    state: engine.State,
    inverse: dict,
    now: datetime,
    receivable_by_id: dict[str, ReceivableModel],
) -> None:
    p = fix.params

    if fix.type == FixType.F1_ADD_COUNTERPARTY_ALIAS:
        alias_row_id = uuid.UUID(p["alias_counterparty_id"])
        alias_counterparty = session.get(CounterpartyModel, alias_row_id)
        assert alias_counterparty is not None
        row = CounterpartyAlias(
            canonical_id=uuid.UUID(p["canonical_counterparty_id"]),
            alias_external_id=alias_counterparty.external_id,
            alias_name=alias_counterparty.name,
            fix_id=fix.id,
            created_at=now,
        )
        session.add(row)
        session.flush()
        _ledger(
            session,
            "system",
            "add_alias",
            "counterparty_alias",
            row.id,
            after={
                "alias": p["alias_counterparty_id"],
                "canonical": p["canonical_counterparty_id"],
            },
        )
        state.aliases[p["alias_counterparty_id"]] = p["canonical_counterparty_id"]

    elif fix.type == FixType.F3_MARK_DUPLICATE:
        for pair in p["pairs"]:
            receipt = session.get(ReceiptModel, uuid.UUID(pair["receipt_id"]))
            assert receipt is not None
            receipt.duplicate_of = uuid.UUID(pair["duplicate_of"])
            _ledger(
                session,
                "system",
                "mark_duplicate",
                "receipt",
                receipt.id,
                after={"duplicate_of": pair["duplicate_of"]},
            )
        return  # F2/F3 change receipt inputs; the rerun below re-decides them

    elif fix.type == FixType.F2_REPAIR_REFERENCE:
        for repair in p["repairs"]:
            receipt = session.get(ReceiptModel, uuid.UUID(repair["receipt_id"]))
            assert receipt is not None
            if receipt.original_reference is None:
                receipt.original_reference = receipt.reference
            receipt.reference = repair["to_ref"]
            _ledger(
                session,
                "system",
                "repair_reference",
                "receipt",
                receipt.id,
                before={"reference": receipt.original_reference},
                after={"reference": repair["to_ref"]},
            )
        return

    elif fix.type == FixType.F4_SPLIT_RECEIPT:
        for split in p["splits"]:
            receipt_id = uuid.UUID(split["receipt_id"])
            # The funding receipt commonly arrives here as an R6 overpayment,
            # which already partially allocated it to one receivable and held
            # the rest as an unapplied credit. Reverse that partial allocation
            # first, or the split below double-books that receivable.
            old_allocs = session.scalars(
                select(AllocationModel).where(AllocationModel.receipt_id == receipt_id)
            ).all()
            for old in old_allocs:
                old_rv = receivable_by_id.get(str(old.receivable_id)) or session.get(
                    ReceivableModel, old.receivable_id
                )
                assert old_rv is not None
                old_rv.allocated -= old.amount
                _recompute_status(old_rv)
                session.delete(old)
            if old_allocs:
                session.flush()
            for a in split["allocations"]:
                rv_row = receivable_by_id[a["receivable_id"]]
                alloc_id = uuid.uuid4()
                session.add(
                    AllocationModel(
                        id=alloc_id,
                        receipt_id=receipt_id,
                        receivable_id=uuid.UUID(a["receivable_id"]),
                        amount=a["amount"],
                        decision_id=None,
                        created_at=now,
                    )
                )
                rv_row.allocated += a["amount"]
                _ledger(
                    session,
                    "system",
                    "allocate",
                    "allocation",
                    alloc_id,
                    after={
                        "receipt_id": split["receipt_id"],
                        "receivable_id": a["receivable_id"],
                        "amount": a["amount"],
                        "fix_id": str(fix.id),
                    },
                )
                _recompute_status(rv_row)

            # The receipt that funded this split was previously escalated
            # (overpayment, or "one receipt pays several receivables") with its
            # own Decision/Exception and possibly a held UnappliedCredit. Close
            # all three, matching what F1/F5 already do for their receipts.
            decision_row = session.scalars(
                select(DecisionModel).where(DecisionModel.receipt_id == receipt_id)
            ).first()
            if decision_row is not None:
                decision_row.outcome = DecisionOutcome.SETTLED
                decision_row.reviewed_by = f"fix:{fix.id}"
                decision_row.reviewed_at = now
                decision_row.review_outcome = "resolved_by_fix"
                exception_row = session.scalars(
                    select(ExceptionModel).where(
                        ExceptionModel.decision_id == decision_row.id,
                        ExceptionModel.status == ExceptionStatus.OPEN,
                    )
                ).first()
                if exception_row is not None:
                    exception_row.status = ExceptionStatus.RESOLVED
                    exception_row.resolved_by = f"fix:{fix.id}"
                    exception_row.resolution = fix.summary or fix.type.value

            held_credits = session.scalars(
                select(UnappliedCreditModel).where(
                    UnappliedCreditModel.receipt_id == receipt_id,
                    UnappliedCreditModel.status == UnappliedCreditStatus.HELD,
                )
            ).all()
            for credit in held_credits:
                credit.status = UnappliedCreditStatus.APPLIED
                _ledger(
                    session,
                    "system",
                    "consume_unapplied_credit",
                    "unapplied_credit",
                    credit.id,
                    after={"fix_id": str(fix.id)},
                )

    elif fix.type == FixType.F5_RECORD_FEE_DEDUCTION:
        # Relabelled in place, not deleted+reinserted: a ClusterMember row points at
        # this exact Adjustment id (it's how grouping found the "absorbed" members),
        # and Adjustment isn't in the append-only-ledger list, but its id is still a
        # live foreign key elsewhere.
        for removed in inverse["removed_adjustments"]:
            row = session.scalars(
                select(AdjustmentModel)
                .join(DecisionModel, AdjustmentModel.decision_id == DecisionModel.id)
                .where(
                    DecisionModel.receipt_id == uuid.UUID(removed["receipt_id"]),
                    AdjustmentModel.receivable_id == uuid.UUID(removed["receivable_id"]),
                    AdjustmentModel.reason == AdjustmentReason(removed["reason"]),
                )
            ).first()
            if row is not None:
                _ledger(
                    session,
                    "system",
                    "relabel_adjustment",
                    "adjustment",
                    row.id,
                    before={"amount": row.amount, "reason": row.reason.value},
                    after={"reason": "fee", "fix_id": str(fix.id)},
                )
                row.reason = AdjustmentReason.FEE
                # amount and receivable balance are unchanged -- only the label moves.
                # fix_id is left as-is (the ledger event above is the audit trail for
                # this relabel); fix_id is reserved for adjustments this fix actually
                # created, which is what the apply-time prediction check sums.
        session.flush()

        # A relabelled receipt (above) already has its full fee adjustment on the
        # books under its old id -- only a previously-escalated receipt (never
        # allocated at all) still needs a new allocation + fee adjustment here.
        relabeled_receipt_ids = {r["receipt_id"] for r in inverse["removed_adjustments"]}
        hint = _receipt_receivable_hint(cluster)
        for rid in p["receipt_ids"]:
            if rid in relabeled_receipt_ids:
                continue
            receipt = session.get(ReceiptModel, uuid.UUID(rid))
            assert receipt is not None
            fee = inverse["fee_amounts"][rid]
            receivable_id = hint[rid]
            rv_row = receivable_by_id[receivable_id]
            alloc_id = uuid.uuid4()
            session.add(
                AllocationModel(
                    id=alloc_id,
                    receipt_id=receipt.id,
                    receivable_id=uuid.UUID(receivable_id),
                    amount=receipt.amount,
                    decision_id=None,
                    created_at=now,
                )
            )
            rv_row.allocated += receipt.amount
            _ledger(
                session,
                "system",
                "allocate",
                "allocation",
                alloc_id,
                after={
                    "receipt_id": rid,
                    "receivable_id": receivable_id,
                    "amount": receipt.amount,
                    "fix_id": str(fix.id),
                },
            )

            adj_id = uuid.uuid4()
            session.add(
                AdjustmentModel(
                    id=adj_id,
                    receivable_id=uuid.UUID(receivable_id),
                    amount=fee,
                    reason=AdjustmentReason.FEE,
                    decision_id=None,
                    fix_id=fix.id,
                    created_at=now,
                )
            )
            rv_row.adjusted += fee
            _ledger(
                session,
                "system",
                "adjust",
                "adjustment",
                adj_id,
                after={
                    "receivable_id": receivable_id,
                    "amount": fee,
                    "reason": "fee",
                    "fix_id": str(fix.id),
                },
            )
            _recompute_status(rv_row)

            decision_row = session.scalars(
                select(DecisionModel).where(DecisionModel.receipt_id == receipt.id)
            ).first()
            if decision_row is not None:
                decision_row.outcome = DecisionOutcome.SETTLED
                decision_row.reviewed_by = f"fix:{fix.id}"
                decision_row.reviewed_at = now
                decision_row.review_outcome = "resolved_by_fix"
                exception_row = session.scalars(
                    select(ExceptionModel).where(
                        ExceptionModel.decision_id == decision_row.id,
                        ExceptionModel.status == ExceptionStatus.OPEN,
                    )
                ).first()
                if exception_row is not None:
                    exception_row.status = ExceptionStatus.RESOLVED
                    exception_row.resolved_by = f"fix:{fix.id}"
                    exception_row.resolution = fix.summary or fix.type.value


def apply_fix(session: Session, fix_id: str) -> ApplyOutcome:
    fix = session.get(Fix, fix_id)
    if fix is None:
        raise FixesError(f"fix {fix_id} not found")
    if fix.status != FixStatus.APPROVED:
        raise FixesError(f"fix must be approved to apply, is {fix.status.value}")

    cluster = session.get(Cluster, fix.cluster_id)
    assert cluster is not None
    run = session.get(RunModel, cluster.run_id)
    assert run is not None

    current_hash, state = _snapshot_for_fix(session, cluster, fix)
    if current_hash != fix.snapshot_hash:
        _ledger(
            session,
            "system",
            "apply_stale",
            "fix",
            fix.id,
            after={"expected": fix.snapshot_hash, "actual": current_hash},
        )
        session.commit()
        return ApplyOutcome(fix=fix, verification=None, stale=True)

    now = datetime.now(UTC)
    hint = _receipt_receivable_hint(cluster)
    _new_state, inverse = engine.apply_to_state(fix.type.value, fix.params, state, hint)

    receivable_by_id: dict[str, ReceivableModel] = {
        str(r.id): r
        for r in session.scalars(
            select(ReceivableModel).where(
                ReceivableModel.id.in_([uuid.UUID(i) for i in state.receivables])
            )
        ).all()
    }

    try:
        if fix.type in (
            FixType.F1_ADD_COUNTERPARTY_ALIAS,
            FixType.F2_REPAIR_REFERENCE,
            FixType.F3_MARK_DUPLICATE,
        ):
            _apply_data_fix(session, fix, cluster, state, inverse, now, receivable_by_id)
            session.flush()
            new_state_after_db = _load_state(session, run)
            before_decisions = decide_all(state)
            after_decisions = decide_all(new_state_after_db)
            scope = _affected_receipt_ids(cluster)
            for rid in scope:
                after_d = after_decisions.get(rid)
                before_d = before_decisions.get(rid)
                if after_d is None or before_d is None:
                    continue
                if (before_d.outcome, before_d.rule_id) != (after_d.outcome, after_d.rule_id):
                    _rewrite_decision(session, run.id, rid, after_d, fix, now, receivable_by_id)

        elif fix.type == FixType.F6_AMEND_POLICY:
            pv_current = session.get(PolicyVersionModel, run.policy_version_id)
            assert pv_current is not None
            max_version = session.scalar(
                select(PolicyVersionModel.version).order_by(PolicyVersionModel.version.desc())
            )
            import copy as _copy

            new_rules = _copy.deepcopy(pv_current.rules)
            new_rules[fix.params["rule_id"]][fix.params["key"]] = fix.params["after"]
            new_pv = PolicyVersionModel(
                version=(max_version or 0) + 1,
                rules=new_rules,
                created_by="system",
                parent_id=pv_current.id,
                fix_id=fix.id,
                created_at=now,
            )
            session.add(new_pv)
            session.flush()
            _ledger(
                session,
                "system",
                "amend_policy",
                "policy_version",
                new_pv.id,
                before={"version": pv_current.version, "rule": fix.params["rule_id"]},
                after={
                    "version": new_pv.version,
                    "key": fix.params["key"],
                    "after": fix.params["after"],
                },
            )
            run.policy_version_id = new_pv.id

            before_decisions = decide_all(state)
            after_state = state.copy()
            after_state.policy = new_rules
            after_decisions = decide_all(after_state)
            for rid, before_d in before_decisions.items():
                after_d = after_decisions.get(rid)
                if after_d is None:
                    continue
                if (before_d.outcome, before_d.rule_id) != (after_d.outcome, after_d.rule_id):
                    _rewrite_decision(session, run.id, rid, after_d, fix, now, receivable_by_id)

        elif fix.type in (FixType.F4_SPLIT_RECEIPT, FixType.F5_RECORD_FEE_DEDUCTION):
            _apply_data_fix(session, fix, cluster, state, inverse, now, receivable_by_id)

        elif fix.type == FixType.F7_MANUAL_REVIEW:
            pass

        session.flush()

        ledger_state = current_ledger_state(session, run, build_aliases)
        invariant_results = check_invariants(ledger_state)
        failures = [r for r in invariant_results if not r.passed]

        predicted = None
        dry_run_row = session.scalars(
            select(DryRunModel)
            .where(DryRunModel.fix_id == fix.id, DryRunModel.fix_version == fix.version)
            .order_by(DryRunModel.created_at.desc())
        ).first()
        prediction_matched = True
        if dry_run_row is not None:
            predicted = dry_run_row.predicted_counts
            actual_applied, actual_written_off = _actual_totals(session, fix, cluster)
            prediction_matched = actual_applied == predicted.get(
                "applied_centavos"
            ) and actual_written_off == predicted.get("written_off_centavos")

        if failures or not prediction_matched:
            raise _VerificationFailed(invariant_results, prediction_matched)

        fix.status = FixStatus.APPLIED
        fix.inverse = inverse
        signature_predicate = _condition_signature(fix)
        if signature_predicate is not None:
            session.add(
                ConditionSignatureModel(
                    fix_id=fix.id, predicate=signature_predicate, created_at=now
                )
            )

        verification = VerificationModel(
            fix_id=fix.id,
            prediction_matched=prediction_matched,
            invariants=[
                {"name": r.name, "passed": r.passed, "detail": r.detail} for r in invariant_results
            ],
            outcome=VerificationOutcome.VERIFIED,
            created_at=now,
        )
        session.add(verification)
        _ledger(session, "system", "apply", "fix", fix.id, after={"inverse": inverse})
        session.commit()
        session.refresh(fix)
        session.refresh(verification)
        return ApplyOutcome(fix=fix, verification=verification)

    except _VerificationFailed as failure:
        session.rollback()
        fix = session.get(Fix, fix_id)
        assert fix is not None
        fix.status = FixStatus.VERIFICATION_FAILED
        session.flush()
        verification = VerificationModel(
            fix_id=fix.id,
            prediction_matched=failure.prediction_matched,
            invariants=[
                {"name": r.name, "passed": r.passed, "detail": r.detail}
                for r in failure.invariant_results
            ],
            outcome=VerificationOutcome.VERIFICATION_FAILED,
            created_at=datetime.now(UTC),
        )
        session.add(verification)
        _ledger(
            session,
            "system",
            "verification_failed",
            "fix",
            fix.id,
            after={
                "invariant_failures": [r.name for r in failure.invariant_results if not r.passed]
            },
        )
        session.commit()
        session.refresh(fix)
        session.refresh(verification)
        return ApplyOutcome(fix=fix, verification=verification)


class _VerificationFailed(Exception):
    def __init__(self, invariant_results, prediction_matched: bool):
        self.invariant_results = invariant_results
        self.prediction_matched = prediction_matched


def _actual_totals(session: Session, fix: Fix, cluster: Cluster) -> tuple[int, int]:
    """Sums scoped to the cluster's own affected receipts, matching what the dry
    run predicted -- not every row this fix's id ended up on. For amend_policy, a
    side effect outside the cluster is tagged with the same fix_id (it's the same
    rerun that resolved it) but was never part of the prediction being checked."""
    receipt_uuids = [uuid.UUID(r) for r in _affected_receipt_ids(cluster)]
    allocations = (
        session.scalars(
            select(AllocationModel).where(AllocationModel.receipt_id.in_(receipt_uuids))
        ).all()
        if receipt_uuids
        else []
    )
    written_offs = (
        session.scalars(
            select(AdjustmentModel)
            .outerjoin(DecisionModel, AdjustmentModel.decision_id == DecisionModel.id)
            .where(
                AdjustmentModel.fix_id == fix.id,
                or_(
                    AdjustmentModel.decision_id.is_(None),
                    DecisionModel.receipt_id.in_(receipt_uuids),
                ),
            )
        ).all()
        if receipt_uuids
        else session.scalars(
            select(AdjustmentModel).where(
                AdjustmentModel.fix_id == fix.id, AdjustmentModel.decision_id.is_(None)
            )
        ).all()
    )
    applied_total = sum(a.amount for a in allocations)
    written_off_total = sum(a.amount for a in written_offs)
    return applied_total, written_off_total
