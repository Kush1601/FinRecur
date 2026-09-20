"""Feature extraction for grouping (spec 3.4 step 4a). Pure: no DB, no SQLAlchemy.
Every feature is computed from a Decision/Exception/Adjustment plus its receipt and
whatever receivable rows the caller hands in as candidates -- fault ids are never
read here. `counterparty_is_new` in particular uses the honest signal ("this
counterparty has no receivables of its own"), not the fault manifest's
`canonical_external_id`."""

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

ItemKind = Literal["exception", "write_off", "rescued"]


@dataclass(frozen=True)
class ReceivableCandidate:
    id: str
    total: int
    shipping: int
    status: str
    seller_ids: tuple[str, ...] = ()
    customer_state: str | None = None


@dataclass(frozen=True)
class GroupingItem:
    """One row grouping 4a considers: an open exception, an auto-applied write-off
    (Adjustment reason short_pay|tolerance), or a rescued R2 match (matched_by
    amount_date, reference non-empty, reference cites no real receivable)."""

    id: str
    kind: ItemKind
    reason_code: str | None
    adjustment_reason: str | None
    receipt_id: str
    receipt_amount: int
    receipt_date: str  # ISO date, for the duplicate fingerprint
    receipt_reference: str
    payment_type: str | None
    counterparty_id: str
    counterparty_has_own_receivables: bool
    gap_centavos: int | None
    matched_receivable: ReceivableCandidate | None = None
    nearby_receivable_ids: tuple[str, ...] = ()
    counterparty_open_receivable_totals: tuple[int, ...] = field(default_factory=tuple)


def _norm_counterparty(name: str) -> str:
    return " ".join("".join(ch for ch in name.lower() if ch.isalnum() or ch.isspace()).split())


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[-1]


def _reference_status(
    reference: str, matched_receivable: ReceivableCandidate | None, nearby_ids: tuple[str, ...]
) -> str:
    if not reference:
        return "empty"
    if (matched_receivable is not None and reference == matched_receivable.id) or (
        reference in nearby_ids
    ):
        return "exact"
    best = min((_edit_distance(reference, cand) for cand in nearby_ids), default=None)
    if best in (1, 2):
        return "edit_distance_1_or_2_from_some_receivable_source_id"
    return "unknown"


def _nearest_reference(
    reference: str, nearby_ids: tuple[str, ...]
) -> tuple[str | None, int | None]:
    """The single closest receivable source id by edit distance, so a repair_reference
    fix has a concrete `to_ref` to propose instead of guessing at one."""
    if not reference or not nearby_ids:
        return None, None
    if reference in nearby_ids:
        return reference, 0
    nearest = min(nearby_ids, key=lambda cand: _edit_distance(reference, cand))
    return nearest, _edit_distance(reference, nearest)


def _shortfall_pct(gap_centavos: int | None, total: int | None) -> float | None:
    if gap_centavos is None or not total:
        return None
    return round((gap_centavos / total) * 100, 2)


def _duplicate_fingerprint(amount: int, date: str, reference: str) -> str:
    return hashlib.sha256(f"{amount}|{date}|{reference}".encode()).hexdigest()


def subset_sum_indices(values: list[int], target: int) -> tuple[int, ...] | None:
    """Find a two- or three-receivable split for the F4 demo shape.

    F4 is deliberately scoped to one transfer covering two or three open orders.
    Searching arbitrary subsets makes an ordinary overpayment exponential in the
    number of historic open rows; this bounded check stays quadratic and makes no
    claim for transfers that need a larger allocation plan.
    """
    usable = [(index, value) for index, value in enumerate(values) if 0 < value <= target]
    by_value: dict[int, list[int]] = defaultdict(list)
    for index, value in usable:
        by_value[value].append(index)

    for index, value in usable:
        for other in by_value.get(target - value, []):
            if other != index:
                return tuple(sorted((index, other)))

    for first_pos, (first_index, first_value) in enumerate(usable):
        for second_index, second_value in usable[first_pos + 1 :]:
            for third_index in by_value.get(target - first_value - second_value, []):
                if third_index not in {first_index, second_index}:
                    return tuple(sorted((first_index, second_index, third_index)))
    return None


def _overpayment_matches(item: GroupingItem) -> bool:
    if item.reason_code != "overpayment":
        return False
    totals = list(item.counterparty_open_receivable_totals)
    if item.matched_receivable is not None:
        totals.append(item.matched_receivable.total)
    return subset_sum_indices(totals, item.receipt_amount) is not None


def compute_features(item: GroupingItem, counterparty_name: str | None = None) -> dict:
    total = item.matched_receivable.total if item.matched_receivable else None
    shipping = item.matched_receivable.shipping if item.matched_receivable else None
    seller_ids = item.matched_receivable.seller_ids if item.matched_receivable else ()
    nearest_id, nearest_distance = _nearest_reference(
        item.receipt_reference, item.nearby_receivable_ids
    )

    return {
        "reason_code": item.reason_code or item.adjustment_reason or "rescued",
        "shortfall_pct": _shortfall_pct(item.gap_centavos, total),
        # the raw gap, since R7's tolerance is defined in centavos, not percent --
        # a 0.01% shortfall on a big order and a 1-centavo shortfall on a tiny one
        # can share a percent while being completely different in absolute terms.
        "shortfall_centavos": item.gap_centavos,
        # named to match spec 3.4 step 4a's feature list; the honest semantics are
        # "shortfall fits within the order's own shipping cost" (review fix 4: a
        # plausibility check, never proof of a freight cause on its own).
        "shortfall_eq_shipping": bool(
            item.gap_centavos is not None
            and shipping is not None
            and shipping > 0
            and item.gap_centavos <= shipping
        ),
        "payment_type": item.payment_type,
        "counterparty_norm": _norm_counterparty(counterparty_name) if counterparty_name else None,
        "counterparty_is_new": not item.counterparty_has_own_receivables,
        "reference_status": _reference_status(
            item.receipt_reference, item.matched_receivable, item.nearby_receivable_ids
        ),
        "nearest_receivable_source_id": nearest_id,
        "nearest_receivable_edit_distance": nearest_distance,
        "duplicate_fingerprint": _duplicate_fingerprint(
            item.receipt_amount, item.receipt_date, item.receipt_reference
        ),
        "seller_id": seller_ids[0] if len(seller_ids) == 1 else None,
        "seller_ids": seller_ids,
        "customer_state": item.matched_receivable.customer_state
        if item.matched_receivable
        else None,
        "receivable_status": item.matched_receivable.status if item.matched_receivable else None,
        "overpayment_matches_multi_receivable": _overpayment_matches(item),
        "kind": item.kind,
        "receipt_id": item.receipt_id,
        "receipt_amount": item.receipt_amount,
        "receipt_reference": item.receipt_reference,
    }
