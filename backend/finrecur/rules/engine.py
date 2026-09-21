"""Rule engine, pure. Order is FINAL: R5 -> R1 -> R2 -> R7 -> R3 -> R4 -> R6 -> R8.

R5 runs as a grouping pass over every receipt whose reference cites a real receivable:
all receipts citing one receivable resolve together against its remaining balance in a
single pass (a single citing receipt is the R1 case). Receipts with no usable reference
fall through to R2, matched one at a time against open receivables of the same
counterparty. No rule module reads a literal threshold -- every number comes from the
policy dict built by seed.py from spec 3.2 (grep-tested: no numeric literal other than
0, 1, 100 anywhere in finrecur/rules/).
"""

from collections import defaultdict

from finrecur.money import pct_of
from finrecur.rules.types import Decision, MatchedBy, ReasonCode
from finrecur.schema import Receipt, Receivable


def run_rules(
    receivables: list[Receivable],
    receipts: list[Receipt],
    aliases: dict[str, str],
    policy: dict,
) -> list[Decision]:
    receivables_by_id = {rv.id: rv for rv in receivables}
    receivables_by_source = {rv.meta.get("source_id", rv.id): rv for rv in receivables}
    remaining = {rv.id: rv.total for rv in receivables}

    ordered = sorted(receipts, key=lambda r: (r.received_at, r.id))

    groups: dict[str, list[Receipt]] = defaultdict(list)
    ungrouped: list[Receipt] = []
    for r in ordered:
        rv = receivables_by_source.get(r.reference) if r.reference else None
        if rv is not None:
            groups[rv.id].append(r)
        else:
            ungrouped.append(r)

    decisions_by_receipt: dict[str, Decision] = {}

    for rv_id, group_receipts in groups.items():
        rv = receivables_by_id[rv_id]
        for d in _resolve_group(rv, group_receipts, remaining, policy):
            decisions_by_receipt[d.receipt_id] = d

    open_by_counterparty: dict[str, list[Receivable]] = defaultdict(list)
    for rv in receivables:
        if rv.status == "open":
            open_by_counterparty[_canonical(rv.counterparty_id, aliases)].append(rv)

    for r in ungrouped:
        d = _resolve_r2(r, open_by_counterparty, remaining, aliases, policy)
        decisions_by_receipt[d.receipt_id] = d

    return [decisions_by_receipt[r.id] for r in ordered]


def _canonical(counterparty_id: str, aliases: dict[str, str]) -> str:
    return aliases.get(counterparty_id, counterparty_id)


def _fingerprint(r: Receipt) -> tuple[int, object, str]:
    return (r.amount, r.received_at.date(), r.reference)


def _base_evidence(r: Receipt, rv: Receivable | None, remaining_before: int) -> dict:
    evidence: dict = {
        "receipt": {
            "id": r.id,
            "amount": r.amount,
            "received_at": r.received_at.isoformat(),
            "reference": r.reference,
        }
    }
    if rv is not None:
        evidence["candidate_receivables"] = [
            {"id": rv.id, "total": rv.total, "shipping": rv.shipping, "remaining": remaining_before}
        ]
    return evidence


def _resolve_group(
    rv: Receivable,
    group_receipts: list[Receipt],
    remaining: dict[str, int],
    policy: dict,
) -> list[Decision]:
    group_receipts = sorted(group_receipts, key=lambda r: (r.received_at, r.id))
    is_group = len(group_receipts) > 1
    rule_id = "R5" if is_group else "R1"
    matched_by: MatchedBy = "group" if is_group else "exact"
    remaining_before = remaining[rv.id]

    if rv.status == "cancelled":
        return [
            Decision(
                receipt_id=r.id,
                outcome="escalated",
                rule_id=rule_id,
                matched_by=matched_by,
                reason_code=ReasonCode.PAYMENT_ON_CANCELLED,
                evidence={
                    **_base_evidence(r, rv, remaining_before),
                    "rules_tried": [
                        {"rule": rule_id, "refused_because": "receivable is cancelled"}
                    ],
                },
            )
            for r in group_receipts
        ]

    fingerprints: dict[tuple, list[Receipt]] = defaultdict(list)
    for r in group_receipts:
        fingerprints[_fingerprint(r)].append(r)
    if any(len(rows) > 1 for rows in fingerprints.values()):
        return [
            Decision(
                receipt_id=r.id,
                outcome="escalated",
                rule_id=rule_id,
                matched_by=matched_by,
                reason_code=ReasonCode.SUSPECTED_DUPLICATE,
                evidence={
                    **_base_evidence(r, rv, remaining_before),
                    "rules_tried": [
                        {
                            "rule": rule_id,
                            "refused_because": "matching amount/date/reference fingerprint "
                            "shared with another receipt in this group",
                        }
                    ],
                },
            )
            for r in group_receipts
        ]

    total_paid = sum(r.amount for r in group_receipts)
    diff = total_paid - remaining_before

    if diff == 0:
        remaining[rv.id] = 0
        return [
            Decision(
                receipt_id=r.id,
                outcome="matched",
                rule_id=rule_id,
                matched_by=matched_by,
                allocations=[(rv.id, r.amount)],
                evidence=_base_evidence(r, rv, remaining_before),
            )
            for r in group_receipts
        ]

    if diff > 0:
        return _apply_overpayment(rv, group_receipts, remaining, matched_by, remaining_before)

    shortfall = -diff
    rules_tried: list[dict] = []

    tolerance = policy["R7"]["tolerance_centavos"]
    if shortfall <= tolerance:
        return _settle_with_adjustment(
            rv,
            group_receipts,
            remaining,
            shortfall,
            "rounding",
            "R7",
            matched_by,
            remaining_before,
        )
    rules_tried.append(
        {"rule": "R7", "refused_because": f"shortfall {shortfall} > tolerance {tolerance}"}
    )

    threshold = policy["R3"]["threshold_centavos"]
    if shortfall <= threshold:
        return _settle_with_adjustment(
            rv,
            group_receipts,
            remaining,
            shortfall,
            "short_pay",
            "R3",
            matched_by,
            remaining_before,
        )
    rules_tried.append(
        {"rule": "R3", "refused_because": f"shortfall {shortfall} > threshold {threshold}"}
    )

    max_percent = policy["R4"]["max_percent"]
    pct = pct_of(shortfall, rv.total)
    fits_shipping = shortfall <= rv.shipping
    if pct <= max_percent and (not policy["R4"]["must_fit_shipping"] or fits_shipping):
        return _settle_with_adjustment(
            rv,
            group_receipts,
            remaining,
            shortfall,
            "tolerance",
            "R4",
            matched_by,
            remaining_before,
        )
    if pct > max_percent:
        rules_tried.append(
            {"rule": "R4", "refused_because": f"shortfall {pct}% > max_percent {max_percent}%"}
        )
    else:
        rules_tried.append(
            {
                "rule": "R4",
                "refused_because": f"shortfall {shortfall} > shipping {rv.shipping}",
            }
        )

    reason_code = ReasonCode.PARTIAL_PAYMENT_PENDING if is_group else ReasonCode.SHORT_PAY
    # R1/R5 only label how the group was formed; R3/R4/R7 tried and refused above
    # (see rules_tried), and R8 is the rule that actually gives up and escalates.
    # Labelling this "R1"/"R5" made recurrence's rule_id-based matching wrong.
    return [
        Decision(
            receipt_id=r.id,
            outcome="escalated",
            rule_id="R8",
            matched_by=matched_by,
            reason_code=reason_code,
            evidence={
                **_base_evidence(r, rv, remaining_before),
                "gap_centavos": shortfall,
                "gap_percent": pct,
                "rules_tried": rules_tried,
            },
        )
        for r in group_receipts
    ]


def _settle_with_adjustment(
    rv: Receivable,
    group_receipts: list[Receipt],
    remaining: dict[str, int],
    shortfall: int,
    reason: str,
    rule_id: str,
    matched_by: MatchedBy,
    remaining_before: int,
) -> list[Decision]:
    remaining[rv.id] = 0
    last_index = len(group_receipts) - 1
    decisions = []
    for i, r in enumerate(group_receipts):
        adjustments = [(rv.id, shortfall, reason)] if i == last_index else []
        decisions.append(
            Decision(
                receipt_id=r.id,
                outcome="matched",
                rule_id=rule_id,
                matched_by=matched_by,
                allocations=[(rv.id, r.amount)],
                adjustments=adjustments,
                evidence={
                    **_base_evidence(r, rv, remaining_before),
                    "gap_centavos": shortfall,
                    "gap_percent": pct_of(shortfall, rv.total),
                },
            )
        )
    return decisions


def _apply_overpayment(
    rv: Receivable,
    group_receipts: list[Receipt],
    remaining: dict[str, int],
    matched_by: MatchedBy,
    remaining_before: int,
) -> list[Decision]:
    capacity = remaining_before
    decisions = []
    for r in group_receipts:
        alloc_amount = min(r.amount, capacity)
        capacity -= alloc_amount
        credit = r.amount - alloc_amount
        allocations = [(rv.id, alloc_amount)] if alloc_amount > 0 else []
        decisions.append(
            Decision(
                receipt_id=r.id,
                outcome="escalated",
                rule_id="R6",
                matched_by=matched_by,
                allocations=allocations,
                unapplied_credit=credit,
                reason_code=ReasonCode.OVERPAYMENT,
                evidence={
                    **_base_evidence(r, rv, remaining_before),
                    "gap_centavos": credit,
                },
            )
        )
    remaining[rv.id] = 0
    return decisions


def _resolve_r2(
    r: Receipt,
    open_by_counterparty: dict[str, list[Receivable]],
    remaining: dict[str, int],
    aliases: dict[str, str],
    policy: dict,
) -> Decision:
    window_days = policy["R2"]["window_days"]
    canonical = _canonical(r.counterparty_id, aliases)
    candidates = [
        rv
        for rv in open_by_counterparty.get(canonical, [])
        if remaining[rv.id] == r.amount
        and rv.issued_at <= r.received_at
        and (r.received_at - rv.issued_at).days <= window_days
    ]

    if len(candidates) == 1:
        rv = candidates[0]
        remaining_before = remaining[rv.id]
        remaining[rv.id] = 0
        return Decision(
            receipt_id=r.id,
            outcome="matched",
            rule_id="R2",
            matched_by="amount_date",
            allocations=[(rv.id, r.amount)],
            evidence=_base_evidence(r, rv, remaining_before),
        )

    reason_code = ReasonCode.MULTIPLE_CANDIDATES if candidates else ReasonCode.NO_MATCH
    evidence: dict = {
        "receipt": {
            "id": r.id,
            "amount": r.amount,
            "received_at": r.received_at.isoformat(),
            "reference": r.reference,
        },
        "candidate_receivables": [
            {
                "id": rv.id,
                "total": rv.total,
                "shipping": rv.shipping,
                "remaining": remaining[rv.id],
            }
            for rv in candidates
        ],
        "rules_tried": [
            {
                "rule": "R2",
                "refused_because": "no open receivable of this counterparty matches amount "
                "and date window"
                if not candidates
                else "more than one open receivable of this counterparty matches amount "
                "and date window",
            }
        ],
    }
    return Decision(
        receipt_id=r.id,
        outcome="escalated",
        rule_id="R2",
        matched_by=None,
        reason_code=reason_code,
        evidence=evidence,
    )
