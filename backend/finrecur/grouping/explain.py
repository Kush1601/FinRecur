"""4b: Claude names the cause for a code-found candidate group (spec 3.4 step 4b).
Input is computed features only -- no free-text memos, no raw Olist fields beyond
what grouping already computed. Row-validated against the group's own member ids
and evidence before anything is trusted; a failure returns None so the caller can
degrade to "Grouped by code · not yet explained" (CLAUDE.md hard rule)."""

import json

from finrecur.claude.cache import cache_key, read_cached, write_cache
from finrecur.claude.client import MODEL, ToolCaller
from finrecur.claude.schemas import ExplainResult

SYSTEM_PROMPT = (
    "You are reviewing reconciliation exceptions for a finance automation system. "
    "You are given a candidate group of receipts that a rule-based grouping step "
    "already bucketed together by shared computed features -- never raw memos or "
    "free text. Name the single shared cause in one plain-English sentence, under "
    "300 characters, precise enough that a human reviewer immediately knows what to "
    "fix (e.g. name the exact payment type and percent for a processor fee, not just "
    "'a fee'). Confirm which member ids genuinely share that cause; you may drop "
    "members that don't fit, but never invent an id that was not given to you. Two "
    "fields describe HOW the system already handled a member, not WHY, and must "
    "never by themselves be a reason to drop it when the underlying numbers match: "
    "(1) `kind` (exception vs write_off) -- one escalated, one was silently written "
    "off; (2) `reason_code`/`adjustment_reason` naming which rule caught it "
    "(no_match, short_pay, tolerance, rounding, overpayment, ...) -- short_pay vs "
    "tolerance vs rounding just says which threshold a write-off happened to clear "
    "first, not a different underlying cause. If members share the same "
    "shortfall_pct and payment_type, they share the cause regardless of `kind` or "
    "`reason_code`/`adjustment_reason`, and all of them must stay confirmed. Cite "
    "only evidence you were given: the name of a feature field (e.g. "
    "'shortfall_pct') or one of its actual values, never anything outside the "
    "input. Give a confidence between 0 and 1."
)

TOOL_NAME = "explain_cluster"
TOOL_SCHEMA = {
    "description": "Report the shared cause of a candidate exception cluster.",
    "type": "object",
    "properties": {
        "cause": {"type": "string", "maxLength": 300},
        "member_ids_confirmed": {"type": "array", "items": {"type": "string"}},
        "member_ids_dropped": {"type": "array", "items": {"type": "string"}},
        "evidence_cited": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "cause",
        "member_ids_confirmed",
        "member_ids_dropped",
        "evidence_cited",
        "confidence",
    ],
}


def _allowed_evidence_tokens(features_by_id: dict[str, dict], member_ids: list[str]) -> set[str]:
    allowed: set[str] = set()
    for mid in member_ids:
        for key, value in features_by_id[mid].items():
            allowed.add(key)
            allowed.add(str(value))
    return allowed


def _evidence_ok(cited: str, allowed: set[str]) -> bool:
    if cited in allowed:
        return True
    tokens = cited.replace(",", " ").replace("=", " ").replace(":", " ").split()
    return any(t in allowed for t in tokens)


def explain(
    member_ids: list[str],
    features_by_id: dict[str, dict],
    computed_summary: str,
    client: ToolCaller | None,
) -> ExplainResult | None:
    input_payload = {
        "computed_summary": computed_summary,
        "members": {mid: features_by_id[mid] for mid in member_ids},
    }
    key = cache_key(MODEL, SYSTEM_PROMPT, TOOL_SCHEMA, input_payload)

    cached = read_cached(key)
    if cached is not None:
        raw = cached
    elif client is not None:
        raw = client.call_tool(
            SYSTEM_PROMPT,
            json.dumps(input_payload, sort_keys=True, default=str),
            TOOL_NAME,
            TOOL_SCHEMA,
        )
        if raw is None:
            return None
        write_cache(key, raw)
    else:
        return None

    try:
        result = ExplainResult.model_validate(raw)
    except Exception:
        return None

    given_ids = set(member_ids)
    if not set(result.member_ids_confirmed).issubset(given_ids):
        return None
    if not set(result.member_ids_dropped).issubset(given_ids):
        return None
    allowed = _allowed_evidence_tokens(features_by_id, member_ids)
    if not all(_evidence_ok(e, allowed) for e in result.evidence_cited):
        return None
    return result
