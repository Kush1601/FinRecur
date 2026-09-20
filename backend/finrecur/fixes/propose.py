"""5: Claude picks a fix from the fixed F1-F7 menu for an explained cluster (spec
3.4 step 5, 3.5). Row-validates every id/amount/before-value against the actual
rows before a human ever sees the proposal; a violation discards the whole result
rather than trying to repair it. Widening bounds (25% move, 5% ceiling, min cluster
size) are step 9's job, not this module's -- this only checks that F6's `before`
matches the policy it was shown.

Review fix (2026-09-20): the first recorded fixtures showed Claude inventing
placeholder ids ("<UNKNOWN>") for F1/F3 params that the old validation only
type-checked, never membership-checked. Every id field is now checked against the
exact candidate ids grouping computed (ProposeContext), and a placeholder string is
rejected outright rather than relying on membership alone to catch it."""

import json
from dataclasses import dataclass, field

from finrecur.claude.cache import cache_key, read_cached, write_cache
from finrecur.claude.client import MODEL, ToolCaller
from finrecur.claude.schemas import ProposeResult

_PLACEHOLDER_STRINGS = {"", "unknown", "tbd", "n/a", "na", "none", "null", "todo", "?"}


def _is_placeholder(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().strip("<>").strip().lower() in _PLACEHOLDER_STRINGS


def _as_number(value: object) -> float | int | None:
    """Claude sometimes quotes a policy number as a string ("2.9" instead of 2.9).
    Compare numerically rather than rejecting a value that's equal but the wrong
    JSON type -- the type gets normalized below once validation passes."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _values_equal(a: object, b: object) -> bool:
    if a == b:
        return True
    na, nb = _as_number(a), _as_number(b)
    return na is not None and nb is not None and na == nb


def _normalize_numeric_params(params: dict, keys: tuple[str, ...]) -> None:
    for key in keys:
        n = _as_number(params.get(key))
        if n is not None:
            params[key] = n


SYSTEM_PROMPT = (
    "You are proposing a fix for a reconciliation exception cluster whose cause has "
    "already been named. Pick exactly one fix type from the fixed menu and fill in "
    "its parameters using ONLY the ids, amounts and policy values given to you in "
    "the input -- never invent an id, and never use a placeholder like '<UNKNOWN>' "
    "or 'TBD'. If you cannot find a real id for a fix type, propose manual_review "
    "instead.\n"
    "\n"
    "Fix menu:\n"
    "F1 add_counterparty_alias {alias_counterparty_id, canonical_counterparty_id} -- "
    "alias_counterparty_id must be the group's own counterparty_id (given per "
    "member); canonical_counterparty_id must be one of the ids listed under that "
    "member's candidate_canonical_counterparties.\n"
    "F2 repair_reference {repairs: [{receipt_id, from_ref, to_ref}]} -- from_ref is "
    "the member's own receipt_reference; to_ref must be exactly that member's "
    "nearest_receivable_source_id.\n"
    "F3 mark_duplicate {pairs: [{receipt_id, duplicate_of}]} -- both ids must be "
    "receipt ids from the input; duplicate_of is the earlier of the two receipts "
    "(the later one is the duplicate, i.e. receipt_id).\n"
    "F4 split_receipt {splits: [{receipt_id, allocations: [{receivable_id, "
    "amount}]}]} -- use exactly the ids and amounts listed in that member's "
    "candidate_receivable_set; allocations must sum to the receipt amount exactly.\n"
    "F5 record_fee_deduction {receipt_ids, fee_percent or fee_fixed_centavos, "
    "payment_type} -- receipt_ids must include EVERY member you were given in this "
    "group, not just the ones that escalated: a write-off member with the same "
    "shortfall_pct and payment_type paid the identical fee, it was just absorbed "
    "instead of escalated, and the fee correction applies to it too.\n"
    "F6 amend_policy {rule_id, key, before, after, justification} -- rule_id and key "
    "must name one line from current_policy_lines below; before must equal that "
    "line's value exactly (never rounded, and as a JSON number if the line's value "
    "is a number -- do not quote it as a string); after must differ from before.\n"
    "F7 manual_review {reason} -- for anything that doesn't fit a specific fix, or "
    "when no real id/candidate is available for the fix that would otherwise apply.\n"
    "\n"
    "Pattern guidance -- check in this order, since more than one pattern can look "
    "similar (a constant percent across one payment_type happens in a fee, a "
    "shipping-adjacent shortfall, AND a rounding-tolerance issue; the size of "
    "shortfall_centavos and shortfall_eq_shipping are what tell them apart -- a "
    "percent alone is not enough, because percent scales with the order size while "
    "a fee, a tolerance, and a rounding gap don't):\n"
    "1. Read the shortfall_centavos field on the members (the absolute centavo "
    "gap, not the percent -- a tiny percent on a large order can still be a large "
    "centavo gap, and vice versa). If shortfall_centavos is only 1-3 for every "
    "member, this takes priority over every other rule below, including rule 2: "
    "it is a rounding-tolerance issue -> amend_policy on R7.tolerance_centavos, a "
    "small move above its current value. Never record_fee_deduction or R4 for a "
    "gap this small; a 'fee' under 1% is not a real processor fee, and a "
    "1-3-centavo gap is not a shipping cost.\n"
    "2. Else if shortfall_eq_shipping is true for the group's members, this is a "
    "shipping-adjacent shortfall, NOT a processor fee, even if the percent is "
    "constant and the payment_type is constant -> amend_policy on R4.max_percent "
    "(a small move above its current value), or manual_review if the move would be "
    "large. Never record_fee_deduction here.\n"
    "3. Otherwise, a constant percent shortfall (at least ~1%) across receipts of "
    "one payment_type -> record_fee_deduction with that percent, for every "
    "receipt_id in the group (see the F5 note above).\n"
    "4. A new counterparty whose receipts match another counterparty's open "
    "receivables by amount and date (see candidate_canonical_counterparties) -> "
    "add_counterparty_alias to that candidate.\n"
    "5. A reference within edit distance 1-2 of a real receivable id "
    "(nearest_receivable_source_id) -> repair_reference.\n"
    "6. An overpayment equal to the sum of several open receivables of the same "
    "counterparty (see candidate_receivable_set) -> split_receipt using exactly "
    "that set.\n"
    "7. Identical amount+date+reference pairs -> mark_duplicate (the later "
    "receipt, by received date, is the duplicate).\n"
    "8. Anything else, or anything you can't back with a real id -> "
    "manual_review.\n"
    "\n"
    "The primary fix must be the one the evidence actually supports; the "
    "alternative is the next most defensible option, not a worse guess. You may "
    "also propose one alternative fix of a different type. Give a short "
    "plain-English summary for each.\n"
    "\n"
    "Required top-level fields on every response, always: fix_type, params, "
    "summary, alternative (an object or null). `summary` describes the PRIMARY "
    "fix_type/params and is required even when you also give an alternative with "
    "its own separate summary -- omitting the top-level summary makes the whole "
    "response invalid and it will be discarded."
)

TOOL_NAME = "propose_fix"
FIX_TYPES = [
    "add_counterparty_alias",
    "repair_reference",
    "mark_duplicate",
    "split_receipt",
    "record_fee_deduction",
    "amend_policy",
    "manual_review",
]
_FIX_OBJECT = {
    "type": "object",
    "properties": {
        "fix_type": {"type": "string", "enum": FIX_TYPES},
        "params": {"type": "object"},
        "summary": {"type": "string"},
    },
    "required": ["fix_type", "params", "summary"],
}
TOOL_SCHEMA = {
    "description": "Propose a fix from the fixed F1-F7 menu for an exception cluster.",
    "type": "object",
    "properties": {
        "fix_type": {"type": "string", "enum": FIX_TYPES},
        "params": {"type": "object"},
        "summary": {"type": "string"},
        "alternative": {"anyOf": [{"type": "null"}, _FIX_OBJECT]},
    },
    "required": ["fix_type", "params", "summary"],
}


def _policy_lines(policy: dict) -> list[str]:
    """The policy as flat `rule_id.key: value` lines -- explicit and unambiguous
    for F6, rather than a nested JSON blob Claude has to parse to find a value."""
    lines = []
    for rule_id in sorted(policy):
        rule = policy[rule_id]
        if not isinstance(rule, dict):
            continue
        for key in sorted(rule):
            lines.append(f"{rule_id}.{key}: {rule[key]!r}")
    return lines


@dataclass(frozen=True)
class ProposeContext:
    """Everything a proposal is allowed to reference. An id, amount, or policy
    target outside this is a validation failure, not something to silently coerce
    or repair."""

    receipt_ids: frozenset[str]
    receivable_ids: frozenset[str]
    receipt_amounts: dict[str, int]
    policy: dict
    group_counterparty_ids: frozenset[str] = frozenset()
    candidate_canonical_ids: frozenset[str] = frozenset()
    nearest_reference_target: dict[str, str] = field(default_factory=dict)


def _validate_one(fix_type: str, params: dict, context: ProposeContext) -> str | None:
    if fix_type == "add_counterparty_alias":
        alias_id = params.get("alias_counterparty_id")
        canonical_id = params.get("canonical_counterparty_id")
        if _is_placeholder(alias_id) or _is_placeholder(canonical_id):
            return "placeholder id in add_counterparty_alias"
        if alias_id not in context.group_counterparty_ids:
            return f"alias_counterparty_id {alias_id!r} is not the group's own counterparty"
        if canonical_id not in context.candidate_canonical_ids:
            return (
                f"canonical_counterparty_id {canonical_id!r} is not one of the supplied candidates"
            )
        return None

    if fix_type == "repair_reference":
        repairs = params.get("repairs")
        if not isinstance(repairs, list) or not repairs:
            return "missing repairs"
        for r in repairs:
            receipt_id = r.get("receipt_id")
            to_ref = r.get("to_ref")
            if (
                _is_placeholder(receipt_id)
                or _is_placeholder(r.get("from_ref"))
                or _is_placeholder(to_ref)
            ):
                return "placeholder value in repair_reference"
            if receipt_id not in context.receipt_ids:
                return f"unknown receipt_id {receipt_id}"
            expected = context.nearest_reference_target.get(receipt_id)
            if expected is None or to_ref != expected:
                return f"to_ref {to_ref!r} does not match the computed nearest id {expected!r}"
        return None

    if fix_type == "mark_duplicate":
        pairs = params.get("pairs")
        if not isinstance(pairs, list) or not pairs:
            return "missing pairs"
        for p in pairs:
            receipt_id = p.get("receipt_id")
            duplicate_of = p.get("duplicate_of")
            if _is_placeholder(receipt_id) or _is_placeholder(duplicate_of):
                return "placeholder id in mark_duplicate"
            if receipt_id not in context.receipt_ids:
                return f"unknown receipt_id {receipt_id}"
            if duplicate_of not in context.receipt_ids:
                return f"unknown duplicate_of {duplicate_of}"
            if receipt_id == duplicate_of:
                return "receipt_id and duplicate_of must differ"
        return None

    if fix_type == "split_receipt":
        splits = params.get("splits")
        if not isinstance(splits, list) or not splits:
            return "missing splits"
        for s in splits:
            receipt_id = s.get("receipt_id")
            if _is_placeholder(receipt_id):
                return "placeholder receipt_id in split_receipt"
            if receipt_id not in context.receipt_ids:
                return f"unknown receipt_id {receipt_id}"
            allocations = s.get("allocations", [])
            if not allocations:
                return f"no allocations for {receipt_id}"
            total = 0
            for a in allocations:
                receivable_id = a.get("receivable_id")
                if _is_placeholder(receivable_id):
                    return "placeholder receivable_id in split_receipt"
                if receivable_id not in context.receivable_ids:
                    return f"unknown receivable_id {receivable_id}"
                total += a.get("amount", 0)
            if total != context.receipt_amounts.get(receipt_id):
                return f"allocations for {receipt_id} do not sum to the receipt amount"
        return None

    if fix_type == "record_fee_deduction":
        receipt_ids = params.get("receipt_ids")
        if not isinstance(receipt_ids, list) or not receipt_ids:
            return "missing receipt_ids"
        for rid in receipt_ids:
            if _is_placeholder(rid):
                return "placeholder receipt_id in record_fee_deduction"
            if rid not in context.receipt_ids:
                return f"unknown receipt_id {rid}"
        if "fee_percent" not in params and "fee_fixed_centavos" not in params:
            return "missing fee_percent or fee_fixed_centavos"
        if _is_placeholder(params.get("payment_type")) or not params.get("payment_type"):
            return "missing payment_type"
        _normalize_numeric_params(params, ("fee_percent", "fee_fixed_centavos"))
        return None

    if fix_type == "amend_policy":
        for key in ("rule_id", "key", "before", "after", "justification"):
            if key not in params:
                return f"missing {key}"
        rule_id, rule_key = params["rule_id"], params["key"]
        if _is_placeholder(rule_id) or _is_placeholder(rule_key):
            return "placeholder rule_id/key in amend_policy"
        if rule_id not in context.policy or rule_key not in context.policy[rule_id]:
            return f"{rule_id}.{rule_key} is not a rule/key in the current policy"
        current = context.policy[rule_id][rule_key]
        if not _values_equal(current, params["before"]):
            return f"before {params['before']!r} does not match current policy value {current!r}"
        if _values_equal(params["after"], params["before"]):
            return "after must differ from before"
        _normalize_numeric_params(params, ("before", "after"))
        return None

    if fix_type == "manual_review":
        if not params.get("reason") or _is_placeholder(params.get("reason")):
            return "missing reason"
        return None

    return f"unknown fix_type {fix_type}"


def _validation_reason(result: ProposeResult, context: ProposeContext) -> str | None:
    reason = _validate_one(result.fix_type, result.params, context)
    if reason is not None:
        return reason
    if result.alternative is not None:
        return _validate_one(result.alternative.fix_type, result.alternative.params, context)
    return None


def propose(
    cause: str,
    confirmed_member_ids: list[str],
    evidence_by_id: dict[str, dict],
    context: ProposeContext,
    client: ToolCaller | None,
) -> ProposeResult | None:
    input_payload = {
        "cause": cause,
        "members": {mid: evidence_by_id[mid] for mid in confirmed_member_ids},
        "current_policy_lines": _policy_lines(context.policy),
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
        result = ProposeResult.model_validate(raw)
    except Exception:
        return None

    if _validation_reason(result, context) is not None:
        return None
    return result
