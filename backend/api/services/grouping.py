"""Loads exceptions/write-offs/rescued decisions for a run, buckets them (step 4a),
persists Cluster/ClusterMember/LedgerEvent rows, and -- when a key is available --
runs Claude's explain/propose pass (step 4b/5) per cluster. Spec 3.4 steps 4-5."""

import asyncio
import uuid
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from api.models import Adjustment as AdjustmentModel
from api.models import (
    AdjustmentReason,
    Cluster,
    ClusterKind,
    ClusterMember,
    ClusterSource,
    ClusterStatus,
    DecisionOutcome,
    Fix,
    FixStatus,
    FixType,
    LedgerEvent,
)
from api.models import Allocation as AllocationModel
from api.models import Counterparty as CounterpartyModel
from api.models import Decision as DecisionModel
from api.models import ExceptionRecord as ExceptionModel
from api.models import PolicyVersion as PolicyVersionModel
from api.models import Receipt as ReceiptModel
from api.models import Receivable as ReceivableModel
from api.models import Run as RunModel
from finrecur.claude.client import ClaudeClient
from finrecur.fixes.propose import ProposeContext
from finrecur.fixes.propose import propose as propose_fix
from finrecur.grouping.bucket import GroupingResult, group
from finrecur.grouping.explain import explain as explain_cluster
from finrecur.grouping.features import (
    GroupingItem,
    ReceivableCandidate,
    compute_features,
    subset_sum_indices,
)

MAX_CONCURRENT_CLAUDE_CALLS = 4


def _to_candidate(row: ReceivableModel) -> ReceivableCandidate:
    return ReceivableCandidate(
        id=row.meta.get("source_id", str(row.id)),
        total=row.total,
        shipping=row.shipping,
        status=row.status.value,
        seller_ids=tuple(row.meta.get("seller_ids", [])),
        customer_state=row.meta.get("customer_state"),
    )


class _Catalog:
    """Everything grouping needs about receivables/counterparties, loaded once per
    call so per-item lookups (edit distance, counterparty totals) don't hit the DB."""

    def __init__(self, session: Session):
        receivable_rows = list(session.scalars(select(ReceivableModel)).all())
        self.by_uuid: dict[str, ReceivableModel] = {str(r.id): r for r in receivable_rows}
        self.by_source_id: dict[str, ReceivableModel] = {
            r.meta.get("source_id", str(r.id)): r for r in receivable_rows
        }
        self.all_source_ids: tuple[str, ...] = tuple(self.by_source_id.keys())

        counts: dict[str, int] = defaultdict(int)
        open_totals: dict[str, list[tuple[str, int]]] = defaultdict(list)
        open_by_amount: dict[int, list[tuple[str, str, datetime]]] = defaultdict(list)
        for r in receivable_rows:
            cid = str(r.counterparty_id)
            counts[cid] += 1
            if r.status.value == "open":
                open_totals[cid].append((str(r.id), r.total))
                open_by_amount[r.total].append((str(r.id), cid, r.issued_at))
        self.receivable_count_by_counterparty = counts
        self.open_receivables_by_counterparty = open_totals
        self.open_receivables_by_amount = open_by_amount

        self.counterparty_names: dict[str, str] = {
            str(c.id): c.name for c in session.scalars(select(CounterpartyModel)).all()
        }

    def candidate_for_uuid(self, receivable_uuid: str) -> ReceivableCandidate | None:
        row = self.by_uuid.get(receivable_uuid)
        return _to_candidate(row) if row is not None else None

    def has_own_receivables(self, counterparty_id: str) -> bool:
        return self.receivable_count_by_counterparty.get(counterparty_id, 0) > 0

    def open_totals_excluding(
        self, counterparty_id: str, exclude_uuid: str | None
    ) -> tuple[int, ...]:
        return tuple(
            total
            for rv_uuid, total in self.open_receivables_by_counterparty.get(counterparty_id, [])
            if rv_uuid != exclude_uuid
        )


def _matching_counterparties_for_amount(
    amount: int, window_days: int, received_date, own_ids: set[str], catalog: _Catalog
) -> set[str]:
    matches: set[str] = set()
    for _rv_uuid, cid, issued_at in catalog.open_receivables_by_amount.get(amount, []):
        if cid in own_ids:
            continue
        issued_date = issued_at.date()
        if issued_date <= received_date and (received_date - issued_date).days <= window_days:
            matches.add(cid)
    return matches


def _candidate_canonical_counterparties(
    group_items: list[GroupingItem], catalog: _Catalog, window_days: int
) -> list[dict]:
    """For a new-counterparty group (fault A's shape): which OTHER counterparties
    have an open receivable matching the group's receipts by amount and an
    R2-style date window, and how many of the group's receipts they'd match. Gives
    Claude a concrete `canonical_counterparty_id` to propose instead of guessing --
    required change 2 from the review.

    Checks two shapes: (1) one receivable per receipt, matched one at a time (a
    repeat customer whose receipts each paid a separate order in full); (2) one
    receivable whose total equals the SUM of every receipt in the group (the fault
    A shape -- several payment rows, e.g. an instalment split across voucher +
    credit card, that together paid one order the variant counterparty never
    should have received)."""
    match_counts: dict[str, int] = defaultdict(int)
    own_ids = {item.counterparty_id for item in group_items}

    for item in group_items:
        received_date = datetime.fromisoformat(item.receipt_date).date()
        for cid in _matching_counterparties_for_amount(
            item.receipt_amount, window_days, received_date, own_ids, catalog
        ):
            match_counts[cid] += 1

    total_amount = sum(item.receipt_amount for item in group_items)
    latest_date = max(datetime.fromisoformat(item.receipt_date).date() for item in group_items)
    for cid in _matching_counterparties_for_amount(
        total_amount, window_days, latest_date, own_ids, catalog
    ):
        match_counts[cid] = max(match_counts[cid], len(group_items))

    return [
        {
            "counterparty_id": cid,
            "name": catalog.counterparty_names.get(cid),
            "matching_receipt_count": count,
        }
        for cid, count in sorted(match_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _candidate_receivable_set(item: GroupingItem, catalog: _Catalog) -> list[dict]:
    """For an overpayment item: a concrete subset of the counterparty's open
    receivables (including the one it settled against) whose totals sum exactly to
    the receipt amount, so F4 split_receipt has real ids and amounts to use instead
    of inventing them."""
    pool: list[tuple[str, int]] = list(
        catalog.open_receivables_by_counterparty.get(item.counterparty_id, [])
    )
    if item.matched_receivable is not None:
        matched_row = catalog.by_source_id.get(item.matched_receivable.id)
        if matched_row is not None and not any(
            rv_uuid == str(matched_row.id) for rv_uuid, _ in pool
        ):
            pool.append((str(matched_row.id), item.matched_receivable.total))

    indices = subset_sum_indices([total for _rv, total in pool], item.receipt_amount)
    if indices is None:
        return []
    return [{"receivable_id": pool[index][0], "amount": pool[index][1]} for index in indices]


def _load_exception_items(session: Session, run_id: str, catalog: _Catalog) -> list[GroupingItem]:
    rows = session.execute(
        select(ExceptionModel, DecisionModel, ReceiptModel)
        .join(DecisionModel, ExceptionModel.decision_id == DecisionModel.id)
        .join(ReceiptModel, DecisionModel.receipt_id == ReceiptModel.id)
        .where(DecisionModel.run_id == run_id, ExceptionModel.status == "open")
    ).all()

    items = []
    for exception, _decision, receipt in rows:
        evidence = exception.evidence
        candidates = evidence.get("candidate_receivables", [])
        matched_uuid = candidates[0]["id"] if len(candidates) == 1 else None
        matched = catalog.candidate_for_uuid(matched_uuid) if matched_uuid else None
        counterparty_id = str(receipt.counterparty_id)
        items.append(
            GroupingItem(
                id=str(exception.id),
                kind="exception",
                reason_code=exception.reason_code.value,
                adjustment_reason=None,
                receipt_id=str(receipt.id),
                receipt_amount=receipt.amount,
                receipt_date=receipt.received_at.date().isoformat(),
                receipt_reference=receipt.reference,
                payment_type=receipt.meta.get("payment_type"),
                counterparty_id=counterparty_id,
                counterparty_has_own_receivables=catalog.has_own_receivables(counterparty_id),
                gap_centavos=evidence.get("gap_centavos"),
                matched_receivable=matched,
                nearby_receivable_ids=catalog.all_source_ids if receipt.reference else (),
                counterparty_open_receivable_totals=catalog.open_totals_excluding(
                    counterparty_id, matched_uuid
                ),
            )
        )
    return items


def _load_write_off_items(session: Session, run_id: str, catalog: _Catalog) -> list[GroupingItem]:
    rows = session.execute(
        select(AdjustmentModel, DecisionModel, ReceiptModel)
        .join(DecisionModel, AdjustmentModel.decision_id == DecisionModel.id)
        .join(ReceiptModel, DecisionModel.receipt_id == ReceiptModel.id)
        .where(
            DecisionModel.run_id == run_id,
            AdjustmentModel.reason.in_([AdjustmentReason.SHORT_PAY, AdjustmentReason.TOLERANCE]),
        )
    ).all()

    items = []
    for adjustment, _decision, receipt in rows:
        matched = catalog.candidate_for_uuid(str(adjustment.receivable_id))
        counterparty_id = str(receipt.counterparty_id)
        items.append(
            GroupingItem(
                id=str(adjustment.id),
                kind="write_off",
                reason_code=None,
                adjustment_reason=adjustment.reason.value,
                receipt_id=str(receipt.id),
                receipt_amount=receipt.amount,
                receipt_date=receipt.received_at.date().isoformat(),
                receipt_reference=receipt.reference,
                payment_type=receipt.meta.get("payment_type"),
                counterparty_id=counterparty_id,
                counterparty_has_own_receivables=catalog.has_own_receivables(counterparty_id),
                gap_centavos=adjustment.amount,
                matched_receivable=matched,
                nearby_receivable_ids=(),
                counterparty_open_receivable_totals=catalog.open_totals_excluding(
                    counterparty_id, str(adjustment.receivable_id)
                ),
            )
        )
    return items


def _load_rescued_items(session: Session, run_id: str, catalog: _Catalog) -> list[GroupingItem]:
    rows = session.execute(
        select(DecisionModel, ReceiptModel)
        .join(ReceiptModel, DecisionModel.receipt_id == ReceiptModel.id)
        .where(
            DecisionModel.run_id == run_id,
            DecisionModel.outcome == DecisionOutcome.SETTLED,
            DecisionModel.matched_by == "amount_date",
        )
    ).all()

    allocations = session.scalars(
        select(AllocationModel).where(AllocationModel.decision_id.in_([d.id for d, _ in rows]))
    ).all()
    receivable_by_decision = {str(a.decision_id): str(a.receivable_id) for a in allocations}

    items = []
    for decision, receipt in rows:
        reference = receipt.reference
        if not reference or reference in catalog.by_source_id:
            continue  # not a "rescued" case: R2 only ever fires with no usable citation
        matched_uuid = receivable_by_decision.get(str(decision.id))
        matched = catalog.candidate_for_uuid(matched_uuid) if matched_uuid else None
        counterparty_id = str(receipt.counterparty_id)
        items.append(
            GroupingItem(
                id=str(decision.id),
                kind="rescued",
                reason_code=None,
                adjustment_reason=None,
                receipt_id=str(receipt.id),
                receipt_amount=receipt.amount,
                receipt_date=receipt.received_at.date().isoformat(),
                receipt_reference=reference,
                payment_type=receipt.meta.get("payment_type"),
                counterparty_id=counterparty_id,
                counterparty_has_own_receivables=catalog.has_own_receivables(counterparty_id),
                gap_centavos=None,
                matched_receivable=matched,
                nearby_receivable_ids=catalog.all_source_ids,
                counterparty_open_receivable_totals=(),
            )
        )
    return items


def _run_grouping(
    session: Session, run_id: str
) -> tuple[GroupingResult, dict[str, GroupingItem], _Catalog]:
    catalog = _Catalog(session)
    items = (
        _load_exception_items(session, run_id, catalog)
        + _load_write_off_items(session, run_id, catalog)
        + _load_rescued_items(session, run_id, catalog)
    )
    items_by_id = {i.id: i for i in items}
    result = group(items, counterparty_names=catalog.counterparty_names)
    return result, items_by_id, catalog


def _member_context(
    item: GroupingItem,
    catalog: _Catalog,
    strategy: str,
    candidate_canonicals: list[dict] | None,
) -> dict:
    matched_uuid = None
    if item.matched_receivable is not None:
        row = catalog.by_source_id.get(item.matched_receivable.id)
        matched_uuid = str(row.id) if row is not None else None
    open_receivables = [
        {"id": rv_uuid, "total": total}
        for rv_uuid, total in catalog.open_receivables_by_counterparty.get(item.counterparty_id, [])
    ]
    ctx: dict = {
        "receivable_id": matched_uuid,
        "counterparty_open_receivables": open_receivables,
        "counterparty_id": item.counterparty_id,
    }
    if strategy == "new_counterparty":
        ctx["candidate_canonical_counterparties"] = candidate_canonicals or []
    if strategy == "overpayment_split":
        ctx["candidate_receivable_set"] = _candidate_receivable_set(item, catalog)
    return ctx


def _replace_open_code_clusters(session: Session, run_id: str) -> None:
    """Idempotent re-grouping: an open code-only cluster with no fix yet is replaced.
    One that Claude has already explained, or that has a fix attached, is left alone."""
    existing = session.scalars(
        select(Cluster).where(
            Cluster.run_id == run_id,
            Cluster.source == ClusterSource.CODE,
            Cluster.status == ClusterStatus.OPEN,
        )
    ).all()
    for cluster in existing:
        has_fix = session.scalar(select(Fix.id).where(Fix.cluster_id == cluster.id).limit(1))
        if has_fix is not None:
            continue
        session.execute(delete(ClusterMember).where(ClusterMember.cluster_id == cluster.id))
        session.delete(cluster)
    session.flush()


def run_grouping_for_run(session: Session, run_id: str) -> list[Cluster]:
    _replace_open_code_clusters(session, run_id)
    result, items_by_id, catalog = _run_grouping(session, run_id)
    policy = _policy_for_run(session, run_id)
    window_days = policy["R2"]["window_days"]

    now = datetime.now(UTC)
    created: list[Cluster] = []
    for g in result.groups:
        # Keyed by receipt id, not by the per-run exception/decision id: receipt ids are
        # stable across re-seeds (finrecur.ids), so the Claude input hash -- and the
        # committed fixture it maps to -- survives `make seed`.
        features = {
            items_by_id[mid].receipt_id: compute_features(
                items_by_id[mid], catalog.counterparty_names.get(items_by_id[mid].counterparty_id)
            )
            for mid in g.member_ids
        }
        candidate_canonicals = None
        if g.strategy == "new_counterparty":
            candidate_canonicals = _candidate_canonical_counterparties(
                [items_by_id[mid] for mid in g.member_ids], catalog, window_days
            )
        context = {
            items_by_id[mid].receipt_id: _member_context(
                items_by_id[mid], catalog, g.strategy, candidate_canonicals
            )
            for mid in g.member_ids
        }

        cluster = Cluster(
            run_id=run_id,
            cause=None,
            confidence=None,
            evidence_cited={
                "strategy": g.strategy,
                "computed_summary": g.computed_summary,
                "features": features,
                "context": context,
                "explanation_status": "not_yet_explained",
            },
            kind=ClusterKind(g.kind),
            source=ClusterSource.CODE,
            status=ClusterStatus.OPEN,
            created_at=now,
        )
        session.add(cluster)
        session.flush()

        for mid in g.member_ids:
            item = items_by_id[mid]
            session.add(
                ClusterMember(
                    cluster_id=cluster.id,
                    exception_id=uuid.UUID(mid) if item.kind == "exception" else None,
                    adjustment_id=uuid.UUID(mid) if item.kind == "write_off" else None,
                    decision_id=uuid.UUID(mid) if item.kind == "rescued" else None,
                )
            )
        session.add(
            LedgerEvent(
                actor="system",
                action="group",
                entity_type="cluster",
                entity_id=cluster.id,
                after={"kind": g.kind, "strategy": g.strategy, "member_count": len(g.member_ids)},
                created_at=now,
            )
        )
        created.append(cluster)

    session.commit()
    return created


def _policy_for_run(session: Session, run_id: str) -> dict:
    run = session.get(RunModel, run_id)
    assert run is not None, f"run {run_id} not found"
    pv = session.get(PolicyVersionModel, run.policy_version_id)
    assert pv is not None, f"policy version {run.policy_version_id} not found"
    return pv.rules


def _compute_explanation(evidence: dict, policy: dict, api_key: str | None):
    """The network-bound half of step 4b/5 -- no session, safe to run off-thread.
    Returns (ExplainResult | None, ProposeResult | None, confirmed_member_ids).

    Claude sees `features` merged with `context` (candidate ids grouping already
    worked out: candidate_canonical_counterparties, candidate_receivable_set,
    nearest_receivable_source_id) so it never has to guess an id -- the review fix
    for the placeholder-id ("<UNKNOWN>") outputs the first fixture recording hit."""
    features_by_id: dict[str, dict] = evidence["features"]
    context_by_id: dict[str, dict] = evidence["context"]
    member_ids = list(features_by_id.keys())
    evidence_by_id = {mid: {**features_by_id[mid], **context_by_id[mid]} for mid in member_ids}
    client = ClaudeClient(api_key) if api_key else None

    result = explain_cluster(
        member_ids, evidence_by_id, evidence.get("computed_summary", ""), client
    )
    if result is None:
        return None, None, []

    confirmed = [mid for mid in result.member_ids_confirmed if mid in features_by_id]
    receipt_ids = {features_by_id[mid]["receipt_id"] for mid in confirmed}
    receipt_amounts = {
        features_by_id[mid]["receipt_id"]: features_by_id[mid]["receipt_amount"]
        for mid in confirmed
    }
    receivable_ids: set[str] = set()
    candidate_canonical_ids: set[str] = set()
    group_counterparty_ids: set[str] = set()
    nearest_reference_target: dict[str, str] = {}
    for mid in confirmed:
        ctx = context_by_id[mid]
        if ctx.get("receivable_id"):
            receivable_ids.add(ctx["receivable_id"])
        for rv in ctx.get("counterparty_open_receivables", []):
            receivable_ids.add(rv["id"])
        for rv in ctx.get("candidate_receivable_set", []):
            receivable_ids.add(rv["receivable_id"])
        for cand in ctx.get("candidate_canonical_counterparties", []):
            candidate_canonical_ids.add(cand["counterparty_id"])
        if ctx.get("counterparty_id"):
            group_counterparty_ids.add(ctx["counterparty_id"])
        nearest_id = features_by_id[mid].get("nearest_receivable_source_id")
        if nearest_id:
            nearest_reference_target[features_by_id[mid]["receipt_id"]] = nearest_id

    propose_context = ProposeContext(
        receipt_ids=frozenset(receipt_ids),
        receivable_ids=frozenset(receivable_ids),
        receipt_amounts=receipt_amounts,
        policy=policy,
        group_counterparty_ids=frozenset(group_counterparty_ids),
        candidate_canonical_ids=frozenset(candidate_canonical_ids),
        nearest_reference_target=nearest_reference_target,
    )
    proposal = propose_fix(result.cause, confirmed, evidence_by_id, propose_context, client)
    return result, proposal, confirmed


def _apply_explanation(session: Session, cluster: Cluster, explain_result, proposal) -> None:
    """The DB-write half of step 4b/5 -- always run on the caller's own thread, one
    cluster at a time, so SQLAlchemy's Session is never touched concurrently."""
    evidence = cluster.evidence_cited
    now = datetime.now(UTC)

    if explain_result is None:
        cluster.evidence_cited = {**evidence, "explanation_status": "not_yet_explained"}
        session.add(
            LedgerEvent(
                actor="system",
                action="explain_skipped",
                entity_type="cluster",
                entity_id=cluster.id,
                after={"reason": "no ANTHROPIC_API_KEY / no cache hit / row validation failed"},
                created_at=now,
            )
        )
        session.commit()
        return

    cluster.cause = explain_result.cause
    cluster.confidence = explain_result.confidence
    cluster.source = ClusterSource.CLAUDE
    cluster.evidence_cited = {
        **evidence,
        "explanation_status": "explained",
        "evidence_cited_by_claude": explain_result.evidence_cited,
        "dropped_member_ids": explain_result.member_ids_dropped,
    }
    session.add(
        LedgerEvent(
            actor="claude",
            action="explain",
            entity_type="cluster",
            entity_id=cluster.id,
            after={
                "cause": explain_result.cause,
                "confidence": explain_result.confidence,
                "dropped": explain_result.member_ids_dropped,
            },
            created_at=now,
        )
    )

    if proposal is not None:
        fix = Fix(
            cluster_id=cluster.id,
            type=FixType(proposal.fix_type),
            version=1,
            params=proposal.params,
            summary=proposal.summary,
            is_widening=False,
            proposed_by="agent",
            is_alternative=False,
            status=FixStatus.PROPOSED,
            created_at=now,
        )
        session.add(fix)
        session.flush()
        session.add(
            LedgerEvent(
                actor="claude",
                action="propose_fix",
                entity_type="fix",
                entity_id=fix.id,
                after={"type": fix.type.value, "params": fix.params},
                created_at=now,
            )
        )
        if proposal.alternative is not None:
            alt = Fix(
                cluster_id=cluster.id,
                type=FixType(proposal.alternative.fix_type),
                version=1,
                params=proposal.alternative.params,
                summary=proposal.alternative.summary,
                is_widening=False,
                proposed_by="agent",
                is_alternative=True,
                status=FixStatus.PROPOSED,
                created_at=now,
            )
            session.add(alt)
            session.flush()
            session.add(
                LedgerEvent(
                    actor="claude",
                    action="propose_fix",
                    entity_type="fix",
                    entity_id=alt.id,
                    after={"type": alt.type.value, "params": alt.params, "is_alternative": True},
                    created_at=now,
                )
            )

    session.commit()


def explain_and_propose_cluster(session: Session, cluster: Cluster, api_key: str | None) -> Cluster:
    """Step 4b + 5 for one cluster. Degrades honestly: without a key and without a
    cache hit, the cluster stays source=code, explanation_status stays
    "not_yet_explained", and no Fix is created (CLAUDE.md hard rule)."""
    policy = _policy_for_run(session, str(cluster.run_id))
    explain_result, proposal, _confirmed = _compute_explanation(
        cluster.evidence_cited, policy, api_key
    )
    _apply_explanation(session, cluster, explain_result, proposal)
    return cluster


async def explain_all_open_code_clusters(
    session: Session, run_id: str, api_key: str | None
) -> list[Cluster]:
    """Runs step 4b/5 for every code-only open cluster of a run, up to
    MAX_CONCURRENT_CLAUDE_CALLS Claude calls in flight at once (spec 3.4 step 5:
    "3-6 parallel calls (Haiku 4.5)"). Only the network-bound computation runs off
    the caller's thread; every DB write happens back on this thread, one cluster at
    a time, since SQLAlchemy's Session isn't safe for concurrent use."""
    clusters = session.scalars(
        select(Cluster).where(
            Cluster.run_id == run_id,
            Cluster.source == ClusterSource.CODE,
            Cluster.status == ClusterStatus.OPEN,
        )
    ).all()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CLAUDE_CALLS)
    policy = _policy_for_run(session, run_id)

    async def _compute(cluster: Cluster):
        async with semaphore:
            return await asyncio.to_thread(
                _compute_explanation, cluster.evidence_cited, policy, api_key
            )

    computed = await asyncio.gather(*[_compute(c) for c in clusters])
    for cluster, (explain_result, proposal, _confirmed) in zip(clusters, computed, strict=True):
        _apply_explanation(session, cluster, explain_result, proposal)
    return list(clusters)
