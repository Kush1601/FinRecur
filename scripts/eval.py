"""Evaluate the current seeded demo run against the hidden fault manifest.

This is deliberately outside ``backend/``: the application never reads the gold
labels. If the database has no Day 1 run, the script creates one, groups it, and
replays committed Claude cache fixtures before scoring.
"""

import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from api.db import SessionLocal  # noqa: E402
from api.models import (  # noqa: E402
    Adjustment,
    Cluster,
    Decision,
    DecisionOutcome,
    ExceptionRecord,
    Fix,
    Receipt,
    Run,
    Verification,
    VerificationOutcome,
)
from api.services.grouping import (  # noqa: E402
    explain_all_open_code_clusters,
    run_grouping_for_run,
)
from api.services.runs import execute_run  # noqa: E402

MANIFEST_PATH = REPO_ROOT / "data" / "faults.json"
JSON_PATH = REPO_ROOT / "eval_report.json"
MARKDOWN_PATH = REPO_ROOT / "eval_report.md"


def _ensure_evaluable_run(session) -> Run:
    run = session.scalars(
        select(Run).where(Run.batch_label == "day1").order_by(Run.created_at.desc())
    ).first()
    if run is None:
        run = execute_run(session, "day1")
    clusters = session.scalars(select(Cluster).where(Cluster.run_id == run.id)).all()
    if not clusters:
        clusters = run_grouping_for_run(session, str(run.id))
    if any(cluster.source.value == "code" for cluster in clusters):
        asyncio.run(explain_all_open_code_clusters(session, str(run.id), None))
    return run


def _outcome(
    decision: Decision, exception: ExceptionRecord | None, adjustments: list[Adjustment]
) -> str:
    if decision.outcome == DecisionOutcome.ESCALATED:
        return f"escalated:{exception.reason_code.value}" if exception else "escalated:missing"
    if adjustments:
        return f"absorbed:{adjustments[0].reason.value}"
    return f"matched:{decision.rule_id}"


def _score(session, run: Run, manifest: dict) -> tuple[dict, list[str]]:
    decisions = session.scalars(select(Decision).where(Decision.run_id == run.id)).all()
    decision_by_receipt = {decision.receipt_id: decision for decision in decisions}
    receipts = session.scalars(select(Receipt).where(Receipt.id.in_(decision_by_receipt))).all()
    exceptions = session.scalars(
        select(ExceptionRecord).where(
            ExceptionRecord.decision_id.in_([decision.id for decision in decisions])
        )
    ).all()
    exception_by_decision = {row.decision_id: row for row in exceptions}
    adjustments = session.scalars(
        select(Adjustment).where(
            Adjustment.decision_id.in_([decision.id for decision in decisions])
        )
    ).all()
    adjustments_by_decision: dict = defaultdict(list)
    for adjustment in adjustments:
        adjustments_by_decision[adjustment.decision_id].append(adjustment)

    rows_by_fault: dict[str, list[tuple[Receipt, Decision]]] = defaultdict(list)
    for receipt in receipts:
        fault_id = receipt.meta.get("fault_id")
        if fault_id:
            rows_by_fault[fault_id].append((receipt, decision_by_receipt[receipt.id]))

    clusters = session.scalars(select(Cluster).where(Cluster.run_id == run.id)).all()
    cluster_receipts = {
        cluster.id: set((cluster.evidence_cited or {}).get("features", {})) for cluster in clusters
    }
    fixes = session.scalars(
        select(Fix).where(Fix.cluster_id.in_([cluster.id for cluster in clusters]))
    ).all()
    fixes_by_cluster: dict = defaultdict(list)
    for fix in fixes:
        if not fix.is_alternative and fix.status.value != "superseded":
            fixes_by_cluster[fix.cluster_id].append(fix)

    expected_fix = {
        "A": "add_counterparty_alias",
        "B": "repair_reference",
        "C": "mark_duplicate",
        "D": "amend_policy",
        "E": "record_fee_deduction",
        "F": "split_receipt",
        "G": "amend_policy",
    }
    clustered_baseline = {"A", "B", "D", "E", "F"}
    failures: list[str] = []
    fault_results = []
    manifest_by_id = {entry["id"]: entry for entry in manifest["faults"]}

    for fault_id, entry in manifest_by_id.items():
        rows = rows_by_fault.get(fault_id, [])
        receipt_ids = {str(receipt.id) for receipt, _decision in rows}
        outcomes = Counter(
            _outcome(
                decision,
                exception_by_decision.get(decision.id),
                adjustments_by_decision.get(decision.id, []),
            )
            for _receipt, decision in rows
        )
        matching_clusters = [
            cluster for cluster in clusters if cluster_receipts[cluster.id] & receipt_ids
        ]
        detected_ids = (
            set().union(
                *(cluster_receipts[cluster.id] & receipt_ids for cluster in matching_clusters)
            )
            if matching_clusters
            else set()
        )
        proposed_types = sorted(
            {
                fix.type.value
                for cluster in matching_clusters
                for fix in fixes_by_cluster.get(cluster.id, [])
            }
        )
        observed_baseline = entry["expected"].get("observed", {})
        if observed_baseline and observed_baseline.get("by_outcome") != dict(outcomes):
            failures.append(f"fault {fault_id}: outcomes changed from milestone baseline")
        if fault_id in clustered_baseline and not matching_clusters:
            failures.append(f"fault {fault_id}: expected a code-found cluster")
        wanted_fix = expected_fix.get(fault_id)
        if fault_id in clustered_baseline and wanted_fix not in proposed_types:
            failures.append(f"fault {fault_id}: missing proposed fix {wanted_fix}")
        fault_results.append(
            {
                "id": fault_id,
                "rows": len(rows),
                "outcomes": dict(outcomes),
                "code_grouped_rows": len(detected_ids),
                "explained": any(cluster.cause for cluster in matching_clusters),
                "expected_fix_type": wanted_fix,
                "proposed_fix_types": proposed_types,
                "fix_type_correct": wanted_fix in proposed_types,
            }
        )

    real_receipt_ids = {str(receipt.id) for receipt in receipts if not receipt.simulated}
    false_clusters = [
        str(cluster.id)
        for cluster in clusters
        if cluster_receipts[cluster.id]
        and cluster_receipts[cluster.id].issubset(real_receipt_ids)
        and cluster.cause is not None
    ]
    verification_rows = session.scalars(select(Verification)).all()
    invariant_failures = sum(
        any(not result.get("passed", False) for result in verification.invariants)
        for verification in verification_rows
    )
    if invariant_failures:
        failures.append(f"{invariant_failures} verification(s) recorded invariant failures")
    if any(
        verification.outcome == VerificationOutcome.VERIFIED and not verification.prediction_matched
        for verification in verification_rows
    ):
        failures.append("a verified fix did not match its dry-run prediction")

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": str(run.id),
        "batch": run.batch_label,
        "counts": run.counts,
        "candidate_groups": len(clusters),
        "explained_groups": sum(cluster.cause is not None for cluster in clusters),
        "false_explained_clusters_on_real_rows": false_clusters,
        "write_offs_by_reason_centavos": dict(
            Counter(
                {
                    reason: sum(a.amount for a in adjustments if a.reason.value == reason)
                    for reason in {a.reason.value for a in adjustments}
                }
            )
        ),
        "verification": {
            "total": len(verification_rows),
            "prediction_matches": sum(row.prediction_matched for row in verification_rows),
            "invariant_failures": invariant_failures,
        },
        "faults": fault_results,
        "gate": {"passed": not failures, "failures": failures},
    }
    return report, failures


def _markdown(report: dict) -> str:
    counts = report["counts"]
    lines = [
        "# FinRecur evaluation",
        "",
        f"Run `{report['run_id']}` · batch `{report['batch']}` · "
        f"generated {report['generated_at']}",
        "",
        f"- Receipts: {counts.get('total_receipts', 0)}",
        f"- Matched: {counts.get('matched', 0)}",
        f"- Escalated: {counts.get('escalated', 0)}",
        f"- Candidate groups: {report['candidate_groups']}",
        f"- Explained groups: {report['explained_groups']}",
        f"- Invariant failures: {report['verification']['invariant_failures']}",
        "",
        "| Fault | Rows | Code grouped | Explained | Expected fix | Proposed fixes |",
        "| --- | ---: | ---: | :---: | --- | --- |",
    ]
    for fault in report["faults"]:
        lines.append(
            f"| {fault['id']} | {fault['rows']} | {fault['code_grouped_rows']} | "
            f"{'yes' if fault['explained'] else 'no'} | {fault['expected_fix_type'] or '—'} | "
            f"{', '.join(fault['proposed_fix_types']) or '—'} |"
        )
    lines.extend(["", f"Gate: **{'PASS' if report['gate']['passed'] else 'FAIL'}**"])
    if report["gate"]["failures"]:
        lines.extend(["", *[f"- {failure}" for failure in report["gate"]["failures"]]])
    return "\n".join(lines) + "\n"


def main() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    with SessionLocal() as session:
        run = _ensure_evaluable_run(session)
        report, failures = _score(session, run, manifest)
    JSON_PATH.write_text(json.dumps(report, indent=2) + "\n")
    MARKDOWN_PATH.write_text(_markdown(report))
    print(_markdown(report), end="")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
