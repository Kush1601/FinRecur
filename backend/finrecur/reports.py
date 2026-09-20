"""Per-run report, code-generated, no LLM. Spec 3.8: processed/matched/escalated,
exceptions by reason_code, write-offs by reason, invariant results, ids for links."""

import hashlib
import json
from collections import defaultdict
from typing import Any


def build_run_report(
    run_id: str,
    policy_version: int,
    batch_label: str,
    decisions: list[dict[str, Any]],
    invariant_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """`decisions` is one dict per Decision row: outcome, reason_code, adjustments
    (list of {amount, reason}), decision_id, exception_id."""
    processed = len(decisions)
    matched = sum(1 for d in decisions if d["outcome"] == "settled")
    escalated = sum(1 for d in decisions if d["outcome"] == "escalated")

    by_reason: dict[str, list[str]] = defaultdict(list)
    for d in decisions:
        if d["outcome"] == "escalated" and d.get("exception_id"):
            by_reason[d["reason_code"]].append(d["exception_id"])

    write_offs: dict[str, int] = defaultdict(int)
    for d in decisions:
        for adj in d.get("adjustments", []):
            write_offs[adj["reason"]] += adj["amount"]

    report = {
        "run_id": run_id,
        "policy_version": policy_version,
        "batch_label": batch_label,
        "processed": processed,
        "matched": matched,
        "escalated": escalated,
        "exceptions_by_reason_code": {
            reason: {"count": len(ids), "exception_ids": ids} for reason, ids in by_reason.items()
        },
        "write_offs_by_reason_centavos": dict(write_offs),
        "invariants": invariant_results,
        "invariants_passed": all(r["passed"] for r in invariant_results),
        "decision_ids": [d["decision_id"] for d in decisions],
    }
    report_hash = hashlib.sha256(
        json.dumps(report, sort_keys=True, default=str).encode()
    ).hexdigest()
    return {**report, "report_hash": report_hash}
