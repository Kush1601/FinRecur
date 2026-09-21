"""Integration tests against real Postgres and the committed Claude fixtures (spec
3.4 step 9-10 test list): E end to end, D two-approver widening, the R7 bounds
rejection, a stale approval, and a forced rollback. `explain_all_open_code_clusters`
is called with api_key=None so every proposal comes from the committed fixture
cache -- no network call, no key needed, deterministic."""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api.db import SessionLocal, engine
from api.main import app
from api.models import (
    Adjustment,
    AdjustmentReason,
    Cluster,
    CounterpartyAlias,
    DryRun,
    ExceptionRecord,
    ExceptionStatus,
    Fix,
    FixStatus,
    FixType,
    LedgerEvent,
)
from api.models import Allocation as AllocationModel
from api.models import Decision as DecisionModel
from api.models import PolicyVersion as PolicyVersionModel
from api.models import Receivable as ReceivableModel
from api.services.grouping import explain_all_open_code_clusters, run_grouping_for_run
from api.services.runs import execute_run
from finrecur.invariants import InvariantResult

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def seeded_run_id() -> str:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "TRUNCATE TABLE ledger_events, recurrence_samples, condition_signatures, "
            "verifications, approvals, dry_runs, cluster_members, fixes, clusters, "
            "exceptions, run_reports, decisions, runs, unapplied_credits, adjustments, "
            "allocations, receipts, receivables, counterparty_aliases, policy_versions, "
            "counterparties RESTART IDENTITY CASCADE"
        )
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "seed.py")],
        cwd=REPO_ROOT / "backend",
        check=True,
    )
    with SessionLocal() as s:
        run = execute_run(s, "day1")
        run_grouping_for_run(s, str(run.id))
        asyncio.run(explain_all_open_code_clusters(s, str(run.id), None))
        s.commit()
        return str(run.id)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _primary_fix(run_id: str, fix_type: FixType, param_filter=None) -> str:
    with SessionLocal() as db:
        rows = db.scalars(
            select(Fix)
            .join(Cluster, Fix.cluster_id == Cluster.id)
            .where(Cluster.run_id == run_id, Fix.type == fix_type, Fix.is_alternative.is_(False))
        ).all()
        for row in rows:
            if param_filter is None or param_filter(row.params):
                return str(row.id)
    raise AssertionError(f"no {fix_type} fix matching filter found for run {run_id}")


# --- E: record_fee_deduction end to end -----------------------------------------


def test_e_fee_deduction_end_to_end(seeded_run_id, client):
    fix_id = _primary_fix(seeded_run_id, FixType.F5_RECORD_FEE_DEDUCTION)

    dry_run = client.post(f"/fixes/{fix_id}/dry-run")
    assert dry_run.status_code == 200, dry_run.text
    body = dry_run.json()
    assert body["predicted"]["matched"] == 11
    assert body["predicted"]["still_failing"] == 0

    repeated = client.post(f"/fixes/{fix_id}/dry-run")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["snapshot_hash"] == body["snapshot_hash"]
    with SessionLocal() as db:
        previews = db.scalars(select(DryRun).where(DryRun.fix_id == fix_id)).all()
        events = db.scalars(
            select(LedgerEvent).where(
                LedgerEvent.entity_id == fix_id, LedgerEvent.action == "dry_run"
            )
        ).all()
        assert len(previews) == 1
        assert len(events) == 1

    approved = client.post(
        f"/fixes/{fix_id}/approve", json={"role": "reviewer", "name": "Reviewer One"}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["fix"]["status"] == "approved"
    assert approved.json()["needs_second_approval"] is False

    applied = client.post(f"/fixes/{fix_id}/apply")
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["fix"]["status"] == "applied"
    assert body["verification"]["outcome"] == "verified"
    assert body["verification"]["prediction_matched"] is True
    assert all(inv["passed"] for inv in body["verification"]["invariants"])

    with SessionLocal() as db:
        fix = db.get(Fix, fix_id)
        assert fix is not None
        receipt_ids = fix.params["receipt_ids"]

        # every one of the 11 members now carries a fee adjustment; the 7 that were
        # silently absorbed are relabeled in place (found by decision_id, since
        # relabeling doesn't set fix_id -- see the comment in _apply_data_fix).
        member_adjustments = db.scalars(
            select(Adjustment)
            .join(DecisionModel, Adjustment.decision_id == DecisionModel.id)
            .where(DecisionModel.receipt_id.in_(receipt_ids))
        ).all()
        assert len(member_adjustments) == 7
        assert all(a.reason == AdjustmentReason.FEE for a in member_adjustments)

        newly_created_fees = db.scalars(
            select(Adjustment).where(
                Adjustment.fix_id == fix_id, Adjustment.reason == AdjustmentReason.FEE
            )
        ).all()
        assert len(newly_created_fees) == 4  # the previously-escalated members

        resolved = db.scalars(
            select(ExceptionRecord)
            .join(DecisionModel, ExceptionRecord.decision_id == DecisionModel.id)
            .where(
                DecisionModel.receipt_id.in_(receipt_ids),
                ExceptionRecord.status == ExceptionStatus.RESOLVED,
            )
        ).all()
        assert len(resolved) == 4
        assert all(r.resolved_by == f"fix:{fix_id}" for r in resolved)


# --- D: widening needs a second, distinct approver ------------------------------


def test_d_widening_needs_second_approver_then_applies(seeded_run_id, client):
    fix_id = _primary_fix(seeded_run_id, FixType.F6_AMEND_POLICY, lambda p: p["rule_id"] == "R4")

    dry_run = client.post(f"/fixes/{fix_id}/dry-run")
    assert dry_run.status_code == 200, dry_run.text
    body = dry_run.json()
    # data/faults.json plants D2 in the (2.9%, 3.6%] band; the cached fixture's
    # proposed amendment is 2.9 -> 3.5, so only the D2 row(s) at or below 3.5%
    # actually get swept in by this specific widening -- at least one does.
    assert len(body["side_effects"]) >= 1

    first = client.post(
        f"/fixes/{fix_id}/approve", json={"role": "reviewer", "name": "Reviewer One"}
    )
    assert first.status_code == 200, first.text
    assert first.json()["needs_second_approval"] is True
    assert first.json()["fix"]["status"] == "dry_run"

    second = client.post(
        f"/fixes/{fix_id}/approve", json={"role": "approver", "name": "Approver Two"}
    )
    assert second.status_code == 200, second.text
    assert second.json()["needs_second_approval"] is False
    assert second.json()["fix"]["status"] == "approved"

    applied = client.post(f"/fixes/{fix_id}/apply")
    assert applied.status_code == 200, applied.text
    assert applied.json()["verification"]["outcome"] == "verified"

    with SessionLocal() as db:
        versions = db.scalars(select(PolicyVersionModel).order_by(PolicyVersionModel.version)).all()
        assert [v.version for v in versions] == [1, 2]
        assert versions[1].parent_id == versions[0].id
        assert str(versions[1].fix_id) == fix_id


def test_r7_amendment_rejected_out_of_bounds(seeded_run_id, client):
    fix_id = _primary_fix(seeded_run_id, FixType.F6_AMEND_POLICY, lambda p: p["rule_id"] == "R7")
    dry_run = client.post(f"/fixes/{fix_id}/dry-run")
    assert dry_run.status_code == 422
    assert "out_of_bounds" in dry_run.json()["detail"]

    fix = client.get(f"/fixes/{fix_id}").json()
    assert fix["status"] == "rejected"

    # A closed chapter -- viewing the cluster again (which triggers a fresh
    # dry-run request) must not silently revive a rejected fix into DRY_RUN.
    # FixesError maps to 404 everywhere else in this router (edit_fix, reject,
    # etc.) for "not in a state this action applies to" -- same here.
    second_attempt = client.post(f"/fixes/{fix_id}/dry-run")
    assert second_attempt.status_code == 404
    assert "rejected" in second_attempt.json()["detail"]
    fix_after = client.get(f"/fixes/{fix_id}").json()
    assert fix_after["status"] == "rejected"


# --- stale approval --------------------------------------------------------------


def test_approve_after_balance_change_returns_stale(seeded_run_id, client):
    fix_id = _primary_fix(seeded_run_id, FixType.F4_SPLIT_RECEIPT)
    client.post(f"/fixes/{fix_id}/dry-run")

    with SessionLocal() as db:
        fix = db.get(Fix, fix_id)
        assert fix is not None
        cluster = db.get(Cluster, fix.cluster_id)
        assert cluster is not None
        context = cluster.evidence_cited["context"]
        some_receivable_id = next(
            iter(v["receivable_id"] for v in context.values() if v.get("receivable_id"))
        )
        rv = db.get(ReceivableModel, some_receivable_id)
        assert rv is not None
        rv.allocated += 1
        rv.remaining -= 1
        db.commit()

    approved = client.post(
        f"/fixes/{fix_id}/approve", json={"role": "reviewer", "name": "Reviewer One"}
    )
    assert approved.status_code == 200
    assert approved.json()["stale"] is True

    with SessionLocal() as db:
        fix = db.get(Fix, fix_id)
        assert fix is not None
        assert fix.status == FixStatus.DRY_RUN  # unchanged


# --- forced rollback ---------------------------------------------------------------


def test_apply_rolls_back_on_invariant_failure(seeded_run_id, client, monkeypatch):
    fix_id = _primary_fix(seeded_run_id, FixType.F1_ADD_COUNTERPARTY_ALIAS)
    client.post(f"/fixes/{fix_id}/dry-run")
    client.post(f"/fixes/{fix_id}/approve", json={"role": "reviewer", "name": "Reviewer One"})

    import api.services.fixes as fixes_module

    monkeypatch.setattr(
        fixes_module,
        "check_invariants",
        lambda state: [
            InvariantResult("forced_failure", False, "monkeypatched for the rollback test")
        ],
    )

    before_alias_count = None
    with SessionLocal() as db:
        before_alias_count = len(db.scalars(select(CounterpartyAlias)).all())
        before_allocation_count = len(db.scalars(select(AllocationModel)).all())

    applied = client.post(f"/fixes/{fix_id}/apply")
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["fix"]["status"] == "verification_failed"
    assert body["verification"]["outcome"] == "verification_failed"
    assert any(not inv["passed"] for inv in body["verification"]["invariants"])

    with SessionLocal() as db:
        assert len(db.scalars(select(CounterpartyAlias)).all()) == before_alias_count
        assert len(db.scalars(select(AllocationModel)).all()) == before_allocation_count
