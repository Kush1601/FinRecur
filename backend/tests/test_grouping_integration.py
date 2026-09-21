"""Integration test against real Postgres (spec 3.4 step 7 + Phase 4 build order
step 7): seed, run day 1, group it, and check the actual clusters against the
milestone-1 observed facts (data/faults.json `expected.observed`, and the milestone
facts about fault B's rescue and fault E's absorbed write-offs)."""

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api.db import SessionLocal, engine
from api.main import app
from api.models import Adjustment, Cluster, ClusterKind, ClusterMember, ExceptionRecord

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def seeded_run():
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
    return run["id"]


@pytest.fixture
def client():
    return TestClient(app)


def _member_keys(session, cluster_id: str) -> set[str]:
    members = session.scalars(
        select(ClusterMember).where(ClusterMember.cluster_id == cluster_id)
    ).all()
    keys = set()
    for m in members:
        if m.exception_id:
            keys.add(str(m.exception_id))
        elif m.adjustment_id:
            keys.add(str(m.adjustment_id))
        elif m.decision_id:
            keys.add(str(m.decision_id))
    return keys


def test_group_route_persists_clusters(client, seeded_run):
    response = client.post(f"/runs/{seeded_run}/group")
    assert response.status_code == 200
    clusters = response.json()
    assert len(clusters) > 0
    for c in clusters:
        assert c["source"] == "code"
        assert c["explanation_status"] == "not_yet_explained"


def test_group_route_is_idempotent(client, seeded_run):
    first = client.post(f"/runs/{seeded_run}/group").json()
    second = client.post(f"/runs/{seeded_run}/group").json()
    assert {c["id"] for c in first} != set()
    # re-grouping replaces open code-only clusters with no fix -- same shapes, new ids
    assert len(first) == len(second)
    assert sorted(c["member_count"] for c in first) == sorted(c["member_count"] for c in second)


def test_fault_e_forms_one_mixed_group_of_eleven(client, seeded_run):
    """Fault E's 11 rows (4 escalated + 7 absorbed, ~2.50% of total) must land in one
    `mixed` group. The real slice also has its own near-zero-shortfall write-offs
    across unrelated counterparties that legitimately cluster into a second, smaller
    `mixed` group by the same strategy -- that's a separate, real finding, not a bug,
    so this only asserts the E-shaped one exists."""
    client.post(f"/runs/{seeded_run}/group")
    clusters = client.get(f"/clusters?run_id={seeded_run}").json()
    mixed = [c for c in clusters if c["kind"] == "mixed"]
    assert mixed
    e_group = next((c for c in mixed if c["member_count"] == 11), None)
    assert e_group is not None, [c["computed_summary"] for c in mixed]
    assert "2.50" in e_group["computed_summary"]
    assert "4 escalated, 7 absorbed" in e_group["computed_summary"]


def test_fault_b_forms_one_rescued_group_of_fourteen(client, seeded_run):
    client.post(f"/runs/{seeded_run}/group")
    clusters = client.get(f"/clusters?run_id={seeded_run}").json()
    rescued = [c for c in clusters if c["kind"] == "rescued"]
    assert len(rescued) == 1
    assert rescued[0]["member_count"] == 14


def test_fault_a_forms_one_new_counterparty_group(client, seeded_run):
    """Milestone-1 observed only 5 of fault A's 18 rows land as open no_match
    exceptions (data/faults.json expected.observed.rows for A) -- the group should
    match what's actually in the DB, not the fault's nominal row count."""
    client.post(f"/runs/{seeded_run}/group")
    clusters = client.get(f"/clusters?run_id={seeded_run}").json()
    escalated = [
        c for c in clusters if c["kind"] == "escalated" and "no_match" in c["computed_summary"]
    ]
    a_group = next((c for c in escalated if c["member_count"] == 5), None)
    assert a_group is not None, [c["computed_summary"] for c in clusters]


def test_fault_d_group_excludes_d2_rows(client, seeded_run):
    client.post(f"/runs/{seeded_run}/group")
    with SessionLocal() as session:
        clusters = session.scalars(select(Cluster).where(Cluster.run_id == seeded_run)).all()
        d_candidates = [
            c for c in clusters if c.evidence_cited.get("strategy") == "seller_shortfall"
        ]
        assert len(d_candidates) == 1, "expected exactly one seller_shortfall group for fault D"
        d_cluster = d_candidates[0]
        assert d_cluster.kind == ClusterKind.ESCALATED
        assert len(d_cluster.evidence_cited["features"]) == 6

        d_exception_ids = {str(e.id) for e in session.scalars(select(ExceptionRecord)).all()}
        member_ids = _member_keys(session, str(d_cluster.id))
        assert member_ids.issubset(d_exception_ids)
        assert len(member_ids) == 6


def test_fault_f_forms_one_overpayment_group_of_three(client, seeded_run):
    client.post(f"/runs/{seeded_run}/group")
    clusters = client.get(f"/clusters?run_id={seeded_run}").json()
    overpayment = [c for c in clusters if "overpayment" in c["computed_summary"]]
    assert any(c["member_count"] == 3 for c in overpayment), [
        c["computed_summary"] for c in clusters
    ]


def test_suspected_duplicate_rows_report(client, seeded_run):
    """C: report how the suspected_duplicate rows actually grouped. Fingerprint
    families here are pairs (original + its duplicate), below the min group size of
    3, so the expectation is they stay singletons -- this test documents that."""
    client.post(f"/runs/{seeded_run}/group")
    with SessionLocal() as session:
        dup_exceptions = session.scalars(
            select(ExceptionRecord).where(ExceptionRecord.reason_code == "suspected_duplicate")
        ).all()
        dup_ids = {str(e.id) for e in dup_exceptions}

        clusters = session.scalars(select(Cluster)).all()
        grouped_dup_ids: set[str] = set()
        for c in clusters:
            member_ids = _member_keys(session, str(c.id))
            grouped_dup_ids |= member_ids & dup_ids

        # Every duplicate row not captured in a >=3 fingerprint family is a
        # singleton; report the split rather than asserting a specific number,
        # since it depends on how many real Olist duplicates share the slice.
        print(
            f"\nsuspected_duplicate rows: {len(dup_ids)} total, "
            f"{len(grouped_dup_ids)} grouped, {len(dup_ids) - len(grouped_dup_ids)} singleton"
        )
        assert len(dup_ids) >= 9  # at least fault C's own 9 rows


def test_payment_on_cancelled_real_rows_are_singletons_or_explained(client, seeded_run):
    """28 unrelated real cancelled-order rows must not form one spurious cluster on
    reason_code alone."""
    client.post(f"/runs/{seeded_run}/group")
    with SessionLocal() as session:
        cancelled_exceptions = session.scalars(
            select(ExceptionRecord).where(ExceptionRecord.reason_code == "payment_on_cancelled")
        ).all()
        cancelled_ids = {str(e.id) for e in cancelled_exceptions}
        clusters = session.scalars(select(Cluster)).all()
        for c in clusters:
            member_ids = _member_keys(session, str(c.id))
            overlap = member_ids & cancelled_ids
            if overlap:
                assert overlap != member_ids, (
                    f"cluster {c.id} is made up solely of payment_on_cancelled rows "
                    "with no other shared feature"
                )


def test_write_off_members_are_adjustments_not_exceptions(client, seeded_run):
    """Fault E's 11-member mixed cluster (not the real slice's smaller 0%-band
    mixed cluster -- picked by size, since cluster iteration order isn't fixed)
    must carry its 7 absorbed rows as ClusterMember.adjustment_id, not
    exception_id."""
    client.post(f"/runs/{seeded_run}/group")
    with SessionLocal() as session:
        mixed_clusters = session.scalars(
            select(Cluster).where(Cluster.run_id == seeded_run, Cluster.kind == ClusterKind.MIXED)
        ).all()
        e_cluster = next(c for c in mixed_clusters if len(c.evidence_cited["features"]) == 11)
        members = session.scalars(
            select(ClusterMember).where(ClusterMember.cluster_id == e_cluster.id)
        ).all()
        adjustment_ids = {m.adjustment_id for m in members if m.adjustment_id}
        assert len(adjustment_ids) == 7  # E's 7 absorbed write-offs
        rows = session.scalars(select(Adjustment).where(Adjustment.id.in_(adjustment_ids))).all()
        assert {r.reason.value for r in rows} <= {"short_pay", "tolerance"}


def test_cluster_detail_returns_member_evidence(client, seeded_run):
    clusters = client.post(f"/runs/{seeded_run}/group").json()
    detail = client.get(f"/clusters/{clusters[0]['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert len(body["members"]) == body["member_count"]
    assert all(member["receipt_id"] for member in body["members"])
    assert all(member["source_receipt_id"] for member in body["members"])
    assert all(member["reference"] for member in body["members"])
    assert all(member["counterparty"] for member in body["members"])
    assert all(member["received_at"] for member in body["members"])
    assert all(member["customer_state"] for member in body["members"])
    assert all(member["seller_locations"] for member in body["members"])
    assert all(member["amount_centavos"] > 0 for member in body["members"])


def test_singleton_exceptions_include_review_context(client, seeded_run):
    client.post(f"/runs/{seeded_run}/group")
    response = client.get(f"/exceptions?run_id={seeded_run}&status=open&singletons_only=true")
    assert response.status_code == 200
    rows = response.json()["items"]
    assert rows
    assert all(row["source_receipt_id"] for row in rows)
    assert all(row["reference"] for row in rows)
    assert all(row["counterparty"] for row in rows)
    assert all(row["amount_centavos"] > 0 for row in rows)
    assert all(row["received_at"] for row in rows)
    assert all(row["customer_state"] for row in rows)


def test_reviewer_can_propose_a_manual_fix_for_a_code_cluster(client, seeded_run):
    cluster = client.post(f"/runs/{seeded_run}/group").json()[0]
    response = client.post(
        f"/clusters/{cluster['id']}/fixes",
        json={
            "type": "manual_review",
            "params": {"reason": "Evidence is inconclusive; keep this cluster with a reviewer."},
            "actor_name": "Reviewer One",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["proposed_by"] == "Reviewer One"
    assert response.json()["type"] == "manual_review"
