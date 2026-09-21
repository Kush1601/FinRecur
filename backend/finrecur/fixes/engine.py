"""Pure fix engine (spec 3.4 step 9). `propose.py` picks a fix and its params;
this module is what actually runs one, both for a dry run (against a copy of the
state) and for a real apply (api/services/fixes.py translates the resulting state
diff into DB writes). No SQLAlchemy, no Claude, no env reads.

`validate` re-checks a fix's params against the current rows before every dry run
and apply -- the fix may have been proposed against data that has since moved on.
It extends the id/sum checks fixes/propose.py already does at proposal time, this
time against the full row set rather than a Claude-supplied candidate list."""

import copy
from dataclasses import dataclass, field, replace
from datetime import datetime

from finrecur.result import Err, Ok, Result

FEE_ADJUSTMENT_REASON = "fee"
ABSORBED_REASONS = ("short_pay", "tolerance")


# --- state -----------------------------------------------------------------------


@dataclass
class StateReceivable:
    id: str
    counterparty_id: str
    total: int
    shipping: int
    allocated: int
    adjusted: int
    remaining: int
    status: str
    issued_at: datetime
    meta: dict = field(default_factory=dict)


@dataclass
class StateReceipt:
    id: str
    counterparty_id: str
    amount: int
    received_at: datetime
    reference: str = ""
    original_reference: str | None = None
    duplicate_of: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class StateAllocation:
    receipt_id: str
    receivable_id: str
    amount: int


@dataclass
class StateAdjustment:
    receivable_id: str
    amount: int
    reason: str
    receipt_id: str | None = None


@dataclass
class State:
    receivables: dict[str, StateReceivable]
    receipts: dict[str, StateReceipt]
    allocations: list[StateAllocation]
    adjustments: list[StateAdjustment]
    aliases: dict[str, str]
    policy: dict

    def copy(self) -> "State":
        return State(
            receivables={k: replace(v, meta=dict(v.meta)) for k, v in self.receivables.items()},
            receipts={k: replace(v, meta=dict(v.meta)) for k, v in self.receipts.items()},
            allocations=[replace(a) for a in self.allocations],
            adjustments=[replace(a) for a in self.adjustments],
            aliases=dict(self.aliases),
            policy=copy.deepcopy(self.policy),
        )


# --- validate --------------------------------------------------------------------


@dataclass(frozen=True)
class Rows:
    """Everything a fix's params are allowed to reference. Built fresh from the
    current DB state before every validate() call -- not the narrower candidate
    list fixes/propose.py offered Claude at proposal time."""

    receipt_ids: frozenset[str]
    receivable_ids: frozenset[str]
    receipt_amounts: dict[str, int]
    policy: dict
    # receipt_id -> the receivable it's allocated to (or would be, per the
    # cluster's own candidate hint, for a receipt that was never allocated).
    # Only populated where known; record_fee_deduction validation uses it to
    # check a proposed fee against the receipt's actual gap.
    receipt_receivable: dict[str, str] = field(default_factory=dict)
    receivable_totals: dict[str, int] = field(default_factory=dict)


def _validate_one(fix_type: str, params: dict, rows: Rows) -> str | None:
    if fix_type == "add_counterparty_alias":
        alias_id = params.get("alias_counterparty_id")
        canonical_id = params.get("canonical_counterparty_id")
        if not alias_id or not canonical_id:
            return "missing alias_counterparty_id/canonical_counterparty_id"
        if alias_id == canonical_id:
            return "alias and canonical counterparty must differ"
        return None

    if fix_type == "repair_reference":
        repairs = params.get("repairs")
        if not isinstance(repairs, list) or not repairs:
            return "missing repairs"
        for r in repairs:
            receipt_id = r.get("receipt_id")
            if receipt_id not in rows.receipt_ids:
                return f"unknown receipt_id {receipt_id}"
            if not r.get("to_ref"):
                return "missing to_ref"
        return None

    if fix_type == "mark_duplicate":
        pairs = params.get("pairs")
        if not isinstance(pairs, list) or not pairs:
            return "missing pairs"
        for p in pairs:
            receipt_id, duplicate_of = p.get("receipt_id"), p.get("duplicate_of")
            if receipt_id not in rows.receipt_ids:
                return f"unknown receipt_id {receipt_id}"
            if duplicate_of not in rows.receipt_ids:
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
            if receipt_id not in rows.receipt_ids:
                return f"unknown receipt_id {receipt_id}"
            allocations = s.get("allocations") or []
            if not allocations:
                return f"no allocations for {receipt_id}"
            total = 0
            for a in allocations:
                if a.get("receivable_id") not in rows.receivable_ids:
                    return f"unknown receivable_id {a.get('receivable_id')}"
                total += a.get("amount", 0)
            if total != rows.receipt_amounts.get(receipt_id):
                return f"allocations for {receipt_id} do not sum to the receipt amount"
        return None

    if fix_type == "record_fee_deduction":
        receipt_ids = params.get("receipt_ids")
        if not isinstance(receipt_ids, list) or not receipt_ids:
            return "missing receipt_ids"
        for rid in receipt_ids:
            if rid not in rows.receipt_ids:
                return f"unknown receipt_id {rid}"
        fee_percent = params.get("fee_percent")
        fee_fixed = params.get("fee_fixed_centavos")
        if fee_percent is None and fee_fixed is None:
            return "missing fee_percent or fee_fixed_centavos"
        if not params.get("payment_type"):
            return "missing payment_type"
        # A fee adjustment must be justified by the actual gap it claims to
        # explain -- balance consistency alone (allocated+adjusted+remaining==
        # total) is satisfied by ANY fee amount, including one that quietly
        # writes off most of the receivable. Reject a fee that doesn't match
        # the receipt's real shortfall within a small rounding tolerance.
        for rid in receipt_ids:
            receivable_id = rows.receipt_receivable.get(rid)
            total = rows.receivable_totals.get(receivable_id) if receivable_id else None
            if total is None:
                continue  # nothing to check the gap against; apply_to_state will skip it too
            gap = total - rows.receipt_amounts[rid]
            if fee_fixed is not None:
                declared = fee_fixed
            else:
                assert fee_percent is not None  # checked above: one of the two is required
                declared = round(total * fee_percent / 100)
            if abs(declared - gap) > 2:
                return (
                    f"fee {declared} for receipt {rid} does not match its actual gap {gap} "
                    "(a fee must explain the shortfall it claims, not just balance the books)"
                )
        return None

    if fix_type == "amend_policy":
        for key in ("rule_id", "key", "before", "after"):
            if key not in params:
                return f"missing {key}"
        rule_id, rule_key = params["rule_id"], params["key"]
        if rule_id not in rows.policy or rule_key not in rows.policy[rule_id]:
            return f"{rule_id}.{rule_key} is not a rule/key in the current policy"
        current = rows.policy[rule_id][rule_key]
        if current != params["before"]:
            return f"before {params['before']!r} does not match current policy value {current!r}"
        if params["after"] == params["before"]:
            return "after must differ from before"
        return None

    if fix_type == "manual_review":
        if not params.get("reason"):
            return "missing reason"
        return None

    return f"unknown fix_type {fix_type}"


def validate(fix_type: str, params: dict, rows: Rows) -> Result[None]:
    reason = _validate_one(fix_type, params, rows)
    return Err(reason) if reason is not None else Ok(None)


# --- apply -------------------------------------------------------------------------


def apply_to_state(
    fix_type: str,
    params: dict,
    state: State,
    receipt_receivable_hint: dict[str, str] | None = None,
) -> tuple[State, dict]:
    """Applies a fix to a COPY of `state` and returns (new_state, inverse_params).
    `receipt_receivable_hint` is only used by record_fee_deduction: for a
    previously-escalated member (never allocated because R3/R4 refused it) it
    names the receivable the receipt was trying to pay, computed by grouping from
    the exception's own evidence -- Claude's params never carry it."""
    new_state = state.copy()
    hint = receipt_receivable_hint or {}

    if fix_type == "add_counterparty_alias":
        alias_id = params["alias_counterparty_id"]
        new_state.aliases[alias_id] = params["canonical_counterparty_id"]
        return new_state, {"alias_counterparty_id": alias_id}

    if fix_type == "repair_reference":
        restore = []
        for r in params["repairs"]:
            receipt = new_state.receipts[r["receipt_id"]]
            restore.append({"receipt_id": r["receipt_id"], "to_ref": receipt.reference})
            if receipt.original_reference is None:
                receipt.original_reference = receipt.reference
            receipt.reference = r["to_ref"]
        return new_state, {"repairs": restore}

    if fix_type == "mark_duplicate":
        restore = [{"receipt_id": p["receipt_id"]} for p in params["pairs"]]
        for p in params["pairs"]:
            new_state.receipts[p["receipt_id"]].duplicate_of = p["duplicate_of"]
        return new_state, {"pairs": restore}

    if fix_type == "split_receipt":
        for s in params["splits"]:
            for a in s["allocations"]:
                rv = new_state.receivables[a["receivable_id"]]
                rv.allocated += a["amount"]
                rv.remaining -= a["amount"]
                new_state.allocations.append(
                    StateAllocation(
                        receipt_id=s["receipt_id"],
                        receivable_id=a["receivable_id"],
                        amount=a["amount"],
                    )
                )
        restore = [{"receipt_id": s["receipt_id"]} for s in params["splits"]]
        return new_state, {"splits": restore}

    if fix_type == "record_fee_deduction":
        return _apply_fee_deduction(params, new_state, hint)

    if fix_type == "amend_policy":
        rule_id, key = params["rule_id"], params["key"]
        new_state.policy[rule_id][key] = params["after"]
        return new_state, {
            "rule_id": rule_id,
            "key": key,
            "before": params["after"],
            "after": params["before"],
            "justification": "revert",
        }

    if fix_type == "manual_review":
        return new_state, {}

    raise ValueError(f"unknown fix_type {fix_type}")


def _apply_fee_deduction(
    params: dict, state: State, receipt_receivable_hint: dict[str, str]
) -> tuple[State, dict]:
    receipt_ids = set(params["receipt_ids"])
    fee_percent = params.get("fee_percent")
    fee_fixed = params.get("fee_fixed_centavos")

    # A member that was silently absorbed had a short_pay/tolerance write-off for
    # the same gap; that write-off is now mislabelled and gets replaced by a fee
    # adjustment further down, not double-counted.
    removed_adjustments = []
    kept = []
    for adj in state.adjustments:
        if adj.receipt_id in receipt_ids and adj.reason in ABSORBED_REASONS:
            rv = state.receivables[adj.receivable_id]
            rv.adjusted -= adj.amount
            rv.remaining += adj.amount
            removed_adjustments.append(
                {
                    "receivable_id": adj.receivable_id,
                    "receipt_id": adj.receipt_id,
                    "amount": adj.amount,
                    "reason": adj.reason,
                }
            )
            continue
        kept.append(adj)
    state.adjustments = kept

    existing_allocation_by_receipt = {a.receipt_id: a for a in state.allocations}
    fee_amounts: dict[str, int] = {}
    new_allocations: list[str] = []

    for rid in params["receipt_ids"]:
        receipt = state.receipts[rid]
        alloc = existing_allocation_by_receipt.get(rid)
        receivable_id = alloc.receivable_id if alloc else receipt_receivable_hint.get(rid)
        if receivable_id is None:
            continue  # nothing to allocate/adjust against; validate() should have caught this
        rv = state.receivables[receivable_id]

        if alloc is None:
            # was escalated, never allocated -- the fee explains the whole gap
            state.allocations.append(
                StateAllocation(receipt_id=rid, receivable_id=receivable_id, amount=receipt.amount)
            )
            rv.allocated += receipt.amount
            rv.remaining -= receipt.amount
            new_allocations.append(rid)
            # Same declared calculation as the already-allocated branch below --
            # validate() has already checked it approximately matches the real
            # gap, so this never becomes "the fee is whatever's left over".
            if fee_fixed is not None:
                fee = fee_fixed
            else:
                assert fee_percent is not None  # validate() requires one of the two
                fee = round(rv.total * fee_percent / 100)
        elif fee_fixed is not None:
            fee = fee_fixed
        else:
            assert fee_percent is not None  # validate() requires one of the two
            fee = round(rv.total * fee_percent / 100)

        rv.adjusted += fee
        rv.remaining -= fee
        state.adjustments.append(
            StateAdjustment(
                receivable_id=receivable_id,
                receipt_id=rid,
                amount=fee,
                reason=FEE_ADJUSTMENT_REASON,
            )
        )
        fee_amounts[rid] = fee

    inverse = {
        "receipt_ids": list(params["receipt_ids"]),
        "removed_adjustments": removed_adjustments,
        "fee_amounts": fee_amounts,
        "new_allocation_receipt_ids": new_allocations,
    }
    return state, inverse
