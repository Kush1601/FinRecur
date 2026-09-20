"""Candidate grouping (spec 3.4 step 4a). Deterministic, no LLM, no DB -- literal
fixtures drive every strategy in isolation. Strategies run in a fixed order and the
first one to claim an item wins it; everything unclaimed at the end is a singleton.
No thresholds are literals except the two named in GroupingConfig (spec's own
carve-out: "put those in a GroupingConfig dataclass")."""

from collections import defaultdict
from dataclasses import dataclass

from finrecur.grouping.features import GroupingItem, compute_features
from finrecur.money import format_brl


@dataclass(frozen=True)
class GroupingConfig:
    shortfall_tolerance_pct: float = 0.01
    min_group_size: int = 3


@dataclass(frozen=True)
class CandidateGroup:
    kind: str
    strategy: str
    member_ids: tuple[str, ...]
    computed_summary: str


@dataclass(frozen=True)
class GroupingResult:
    groups: tuple[CandidateGroup, ...]
    singletons: tuple[str, ...]


def _cluster_by_value(pairs: list[tuple[str, float]], tol: float) -> list[list[str]]:
    """Chained-tolerance clustering: sort by value, start a new cluster whenever the
    gap to the previous item exceeds `tol`. Not a general clustering algorithm --
    built for the tight ~2.50% band fault E is constructed around (spec milestone-1
    facts: 2.495-2.502 after centavo rounding)."""
    ordered = sorted(pairs, key=lambda p: p[1])
    clusters: list[list[str]] = []
    current: list[str] = []
    prev_value: float | None = None
    for item_id, value in ordered:
        if current and prev_value is not None and value - prev_value > tol:
            clusters.append(current)
            current = []
        current.append(item_id)
        prev_value = value
    if current:
        clusters.append(current)
    return clusters


def _kind_from_members(member_ids: list[str], features: dict[str, dict]) -> str:
    kinds = {features[i]["kind"] for i in member_ids}
    if kinds == {"write_off"}:
        return "absorbed"
    if kinds == {"exception"}:
        return "escalated"
    if kinds == {"rescued"}:
        return "rescued"
    return "mixed"


# A shortfall percent is only meaningful for reasons that are actually a shortfall
# against a receivable's total -- an overpayment's gap is a credit, not a gap, and
# showing "shortfall N%" on that group would misrepresent it.
_SHORTFALL_REASONS = {"short_pay", "tolerance", "rounding", "fee"}


def _summary(member_ids: list[str], features: dict[str, dict]) -> str:
    n = len(member_ids)
    reasons = {features[i]["reason_code"] for i in member_ids}
    pcts = [
        features[i]["shortfall_pct"]
        for i in member_ids
        if features[i]["shortfall_pct"] is not None
        and features[i]["reason_code"] in _SHORTFALL_REASONS
    ]
    payment_types = {features[i]["payment_type"] for i in member_ids if features[i]["payment_type"]}
    escalated = sum(1 for i in member_ids if features[i]["kind"] == "exception")
    absorbed = sum(1 for i in member_ids if features[i]["kind"] == "write_off")
    total_amount = sum(features[i]["receipt_amount"] for i in member_ids)

    parts = [f"{n} receipts"]
    if len(reasons) == 1:
        parts.append(next(iter(reasons)))
    if pcts:
        parts.append(f"shortfall {sum(pcts) / len(pcts):.2f}%")
    if len(payment_types) == 1:
        parts.append(next(iter(payment_types)))
    if escalated and absorbed:
        parts.append(f"{escalated} escalated, {absorbed} absorbed")
    parts.append(format_brl(total_amount))
    return " · ".join(parts)


def _make_group(strategy: str, member_ids: list[str], features: dict[str, dict]) -> CandidateGroup:
    return CandidateGroup(
        kind=_kind_from_members(member_ids, features),
        strategy=strategy,
        member_ids=tuple(sorted(member_ids)),
        computed_summary=_summary(member_ids, features),
    )


def _strategy_shortfall_ratio(
    remaining: set[str], features: dict[str, dict], config: GroupingConfig
) -> list[CandidateGroup]:
    """(1) mixed/absorbed/escalated by shortfall_pct within tolerance and the same
    payment_type -- catches fault E (2.5% processor fee) as one group regardless of
    whether each row escalated or was silently absorbed."""
    groups = []
    by_payment_type: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for iid in remaining:
        f = features[iid]
        if f["shortfall_pct"] is not None and f["payment_type"]:
            by_payment_type[f["payment_type"]].append((iid, f["shortfall_pct"]))
    for pairs in by_payment_type.values():
        for cluster in _cluster_by_value(pairs, config.shortfall_tolerance_pct):
            if len(cluster) >= config.min_group_size:
                groups.append(_make_group("shortfall_ratio", cluster, features))
    return groups


def _strategy_rescued(
    remaining: set[str], features: dict[str, dict], config: GroupingConfig
) -> list[CandidateGroup]:
    """(2) rescued: R2 amount+date matches whose reference is a near-miss on some
    real receivable id -- fault B's reference corruption."""
    matches = [
        iid
        for iid in remaining
        if features[iid]["kind"] == "rescued"
        and features[iid]["reference_status"]
        == "edit_distance_1_or_2_from_some_receivable_source_id"
    ]
    if len(matches) < config.min_group_size:
        return []
    return [_make_group("rescued_reference", matches, features)]


def _strategy_new_counterparty(
    remaining: set[str], features: dict[str, dict], config: GroupingConfig
) -> list[CandidateGroup]:
    """(3) escalated: same reason_code + a brand-new counterparty -- fault A."""
    by_reason: dict[str, list[str]] = defaultdict(list)
    for iid in remaining:
        f = features[iid]
        if f["kind"] == "exception" and f["counterparty_is_new"]:
            by_reason[f["reason_code"]].append(iid)
    return [
        _make_group("new_counterparty", ids, features)
        for ids in by_reason.values()
        if len(ids) >= config.min_group_size
    ]


def _strategy_seller_shortfall(
    remaining: set[str], features: dict[str, dict], config: GroupingConfig
) -> list[CandidateGroup]:
    """(4) escalated: same reason_code + a seller in common + shortfall within the
    order's own shipping -- fault D. A multi-seller cart can share a seller with
    another item without the two orders being otherwise identical, so this keys on
    any seller the order shares rather than requiring a single-seller order. D2
    sits on different orders (and sellers) so it can't join this bucket even though
    its shortfall percent overlaps D's band."""
    by_key: dict[tuple[str, str], list[str]] = defaultdict(list)
    for iid in remaining:
        f = features[iid]
        if f["kind"] != "exception" or not f["shortfall_eq_shipping"]:
            continue
        for seller_id in f["seller_ids"]:
            by_key[(f["reason_code"], seller_id)].append(iid)
    groups = []
    claimed: set[str] = set()
    for ids in by_key.values():
        available = [i for i in ids if i not in claimed]
        if len(available) >= config.min_group_size:
            groups.append(_make_group("seller_shortfall", available, features))
            claimed.update(available)
    return groups


def _strategy_duplicate_fingerprint(
    remaining: set[str], features: dict[str, dict], config: GroupingConfig
) -> list[CandidateGroup]:
    """(5) suspected_duplicate rows bucketed by the amount+date+reference fingerprint.
    One group per fingerprint family -- real and injected duplicates only land in the
    same group if their fingerprint genuinely matches, never merged on reason_code
    alone."""
    by_fp: dict[str, list[str]] = defaultdict(list)
    for iid in remaining:
        f = features[iid]
        if f["reason_code"] == "suspected_duplicate":
            by_fp[f["duplicate_fingerprint"]].append(iid)
    return [
        _make_group("duplicate_fingerprint", ids, features)
        for ids in by_fp.values()
        if len(ids) >= config.min_group_size
    ]


def _strategy_overpayment(
    remaining: set[str], features: dict[str, dict], config: GroupingConfig
) -> list[CandidateGroup]:
    """(6) overpayment where the receipt amount equals the sum of >=2 of the
    counterparty's open receivables -- fault F (one transfer, several orders)."""
    ids = [
        iid
        for iid in remaining
        if features[iid]["reason_code"] == "overpayment"
        and features[iid]["overpayment_matches_multi_receivable"]
    ]
    if len(ids) < config.min_group_size:
        return []
    return [_make_group("overpayment_split", ids, features)]


_STRATEGIES = (
    _strategy_shortfall_ratio,
    _strategy_rescued,
    _strategy_new_counterparty,
    _strategy_seller_shortfall,
    _strategy_duplicate_fingerprint,
    _strategy_overpayment,
)


def group(
    items: list[GroupingItem],
    config: GroupingConfig | None = None,
    counterparty_names: dict[str, str] | None = None,
) -> GroupingResult:
    config = config or GroupingConfig()
    counterparty_names = counterparty_names or {}
    features = {
        item.id: compute_features(item, counterparty_names.get(item.counterparty_id))
        for item in items
    }
    remaining = {item.id for item in items}
    groups: list[CandidateGroup] = []

    for strategy in _STRATEGIES:
        for candidate in strategy(remaining, features, config):
            member_set = set(candidate.member_ids)
            if not member_set.issubset(remaining):
                continue  # defensive; strategies only ever see `remaining`
            groups.append(candidate)
            remaining -= member_set

    return GroupingResult(
        groups=tuple(sorted(groups, key=lambda g: (g.strategy, g.member_ids))),
        singletons=tuple(sorted(remaining)),
    )
