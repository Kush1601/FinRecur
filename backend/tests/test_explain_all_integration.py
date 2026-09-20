"""Integration tests for POST /runs/{id}/explain-all against the seeded day-1 run.

Two scenarios, both with ANTHROPIC_API_KEY unset (monkeypatched, regardless of
whatever is in the real .env) so neither test depends on a live key or network
call:
1. With the committed cache fixtures in place (backend/tests/fixtures/claude/),
   explain-all must replay from cache and reach the same causes/fixes recorded
   when this repo last ran it with a real key -- CI never needs a key.
2. With no cache hit possible either (an empty cache dir), the workflow must
   still run and degrade honestly: clusters stay source=code, explanation_status
   "not_yet_explained", no Fix rows (CLAUDE.md hard rule)."""

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api.db import SessionLocal, engine
from api.main import app
from api.models import Cluster, ClusterSource, Fix, FixType, LedgerEvent
from api.settings import settings

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def seeded_grouped_run():
    """Function-scoped (not module-scoped): each test calls explain-all once and
    flips every cluster to source=claude, so a shared run would leave nothing left
    to explain for the second test."""
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
    client = TestClient(app)
    run = client.post("/runs", json={"batch": "day1"}).json()
    client.post(f"/runs/{run['id']}/group")
    return run["id"]


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def no_api_key(monkeypatch):
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)


def test_explain_all_replays_from_committed_cache_without_a_key(
    client, seeded_grouped_run, no_api_key
):
    response = client.post(f"/runs/{seeded_grouped_run}/explain-all")
    assert response.status_code == 200
    clusters = {c["member_count"]: c for c in response.json()}

    assert clusters[5]["source"] == "claude"
    assert "add_counterparty_alias" in _fix_types(seeded_grouped_run, clusters[5]["id"])

    assert clusters[3]["source"] == "claude"
    assert "split_receipt" in _fix_types(seeded_grouped_run, clusters[3]["id"])

    assert clusters[14]["source"] == "claude"
    assert "repair_reference" in _fix_types(seeded_grouped_run, clusters[14]["id"])

    assert clusters[6]["source"] == "claude"
    assert "amend_policy" in _fix_types(seeded_grouped_run, clusters[6]["id"])

    mixed = [c for c in response.json() if c["kind"] == "mixed"]
    e_cluster = next(c for c in mixed if c["member_count"] == 11)
    assert e_cluster["source"] == "claude"
    e_fixes = _fixes(seeded_grouped_run, e_cluster["id"])
    primary = next(f for f in e_fixes if not f.is_alternative)
    assert primary.type == FixType.F5_RECORD_FEE_DEDUCTION
    assert primary.params["fee_percent"] == 2.5
    assert primary.params["payment_type"] == "credit_card"
    assert len(primary.params["receipt_ids"]) == 11


def _fixes(run_id: str, cluster_id: str) -> list[Fix]:
    with SessionLocal() as session:
        return list(session.scalars(select(Fix).where(Fix.cluster_id == cluster_id)).all())


def _fix_types(run_id: str, cluster_id: str) -> set[str]:
    return {f.type.value for f in _fixes(run_id, cluster_id)}


def test_explain_all_without_a_key_or_cache_hit_degrades_honestly(
    client, seeded_grouped_run, no_api_key, tmp_path, monkeypatch
):
    from finrecur.claude import cache as cache_module

    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path)

    response = client.post(f"/runs/{seeded_grouped_run}/explain-all")
    assert response.status_code == 200
    clusters = response.json()
    assert clusters
    for c in clusters:
        assert c["source"] == "code"
        assert c["explanation_status"] == "not_yet_explained"
        assert c["cause"] is None

    with SessionLocal() as session:
        db_clusters = session.scalars(
            select(Cluster).where(Cluster.run_id == seeded_grouped_run)
        ).all()
        assert all(c.source == ClusterSource.CODE for c in db_clusters)
        fixes = session.scalars(
            select(Fix).where(Fix.cluster_id.in_([c.id for c in db_clusters]))
        ).all()
        assert fixes == []
        skipped_events = session.scalars(
            select(LedgerEvent).where(LedgerEvent.action == "explain_skipped")
        ).all()
        assert len(skipped_events) == len(db_clusters)
