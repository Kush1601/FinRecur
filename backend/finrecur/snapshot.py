"""Fingerprint of the live state a fix's approval is bound to (spec 3.6). Computed
at dry-run and again inside the apply transaction; a mismatch refuses the apply
(and refuses an approval recorded against a hash that's gone stale). Detects
staleness only -- correctness is the dry run + invariants, accountability is the
Approval record."""

import hashlib
import json


def snapshot_hash(state_for_affected_rows: dict, policy_rules: dict) -> str:
    canonical = {"state": state_for_affected_rows, "policy": policy_rules}
    blob = json.dumps(canonical, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()
