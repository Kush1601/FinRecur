"""Integration test against real Postgres (spec step 6): POST /runs day1, then check
the summary against the DB directly, every escalation has an exception, invariants
passed, and the report hash is stable across two GETs."""

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from api.db import SessionLocal, engine
from api.main import app
from api.models import Decision, DecisionOutcome, ExceptionRecord, Receipt

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def seeded_db():
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


@pytest.fixture
def client():
    return TestClient(app)


def test_post_runs_day1_counts_match_db(seeded_db, client):
    response = client.post("/runs", json={"batch": "day1"})
    assert response.status_code == 200
    body = response.json()

    with SessionLocal() as db:
        run_id = body["id"]
        matched_in_db = db.scalar(
            select(func.count())
            .select_from(Decision)
            .where(Decision.run_id == run_id, Decision.outcome == DecisionOutcome.SETTLED)
        )
        escalated_in_db = db.scalar(
            select(func.count())
            .select_from(Decision)
            .where(Decision.run_id == run_id, Decision.outcome == DecisionOutcome.ESCALATED)
        )
        assert body["counts"]["matched"] == matched_in_db
        assert body["counts"]["escalated"] == escalated_in_db

        escalated_decision_ids = set(
            db.scalars(
                select(Decision.id).where(
                    Decision.run_id == run_id, Decision.outcome == DecisionOutcome.ESCALATED
                )
            ).all()
        )
        decision_ids_with_exception = set(
            db.scalars(
                select(ExceptionRecord.decision_id).where(
                    ExceptionRecord.decision_id.in_(escalated_decision_ids)
                )
            ).all()
        )
        assert escalated_decision_ids == decision_ids_with_exception


def test_report_hash_stable_across_two_gets(seeded_db, client):
    run = client.post("/runs", json={"batch": "day1"}).json()

    first = client.get(f"/runs/{run['id']}/report")
    second = client.get(f"/runs/{run['id']}/report")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["report_hash"] == second.json()["report_hash"]
    assert first.json()["invariants_passed"] is True


def test_day2_loads_with_withheld_day1_counterparties(seeded_db, client):
    response = client.post("/runs", json={"batch": "day2"})
    assert response.status_code == 200, response.text
    assert response.json()["batch_label"] == "day2"

    with SessionLocal() as db:
        day2_receipts = db.scalars(
            select(Receipt).where(Receipt.meta["batch"].astext == "day2")
        ).all()
        assert len(day2_receipts) == 209
