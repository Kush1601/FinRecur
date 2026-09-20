"""Deterministic fault injection over an ImportBundle. Pure functions: bundle in,
new bundle out, nothing mutated in place. Every touched row is `simulated=True` with
`meta.fault_id` set. Read only by scripts/seed.py and scripts/eval.py (spec 3.10) — never
by the app itself.
"""

import random
from collections import defaultdict
from dataclasses import replace

from finrecur.importers import ImportBundle
from finrecur.schema import Counterparty, Receipt, Receivable

R3_THRESHOLD_CENTAVOS = 500  # R$5
R4_MAX_PERCENT = 2.9


def _tag(meta: dict, fault_id: str, **extra) -> dict:
    return {**meta, "fault_id": fault_id, **extra}


def apply_fault_a(bundle: ImportBundle, rng: random.Random, count: int = 18) -> ImportBundle:
    """A: a repeat customer's receipts arrive under a brand-new counterparty variant with
    a blank reference — no rule can match them (no ref for R1/R5, wrong customer for R2)."""
    by_counterparty: dict[str, list[Receipt]] = defaultdict(list)
    for r in bundle.receipts:
        by_counterparty[r.counterparty_id].append(r)

    donor_id = max(by_counterparty, key=lambda cid: len(by_counterparty[cid]))
    donor = next(c for c in bundle.counterparties if c.id == donor_id)
    candidates = sorted(by_counterparty[donor_id], key=lambda r: r.id)
    picked = candidates[:count]
    picked_ids = {r.id for r in picked}

    variant = Counterparty(
        id=f"{donor.external_id}-variant",
        external_id=f"{donor.external_id}-variant",
        name="ACME HOLDINGS LTDA",
        simulated=True,
        meta=_tag({}, "A", canonical_external_id=donor.external_id),
    )

    new_receipts = []
    for r in bundle.receipts:
        if r.id in picked_ids:
            new_receipts.append(
                r.model_copy(
                    update={
                        "counterparty_id": variant.id,
                        "reference": "",
                        "simulated": True,
                        "meta": _tag(r.meta, "A"),
                    }
                )
            )
        else:
            new_receipts.append(r)

    return replace(
        bundle,
        counterparties=[*bundle.counterparties, variant],
        receipts=new_receipts,
        stats={**bundle.stats, "fault_a_rows": len(picked)},
    )


def apply_fault_b(bundle: ImportBundle, rng: random.Random, count: int = 14) -> ImportBundle:
    """B: one character of the reference is dropped or swapped. Original kept for the fix."""
    candidates = sorted((r for r in bundle.receipts if r.reference), key=lambda r: r.id)[:count]
    picked_ids = {r.id for r in candidates}

    def corrupt(ref: str) -> str:
        if len(ref) < 2:
            return ref[::-1]
        i = rng.randrange(len(ref) - 1)
        chars = list(ref)
        if rng.random() < 0.5:
            del chars[i]  # drop
        else:
            chars[i], chars[i + 1] = chars[i + 1], chars[i]  # swap
        return "".join(chars)

    new_receipts = []
    for r in bundle.receipts:
        if r.id in picked_ids:
            new_receipts.append(
                r.model_copy(
                    update={
                        "reference": corrupt(r.reference),
                        "original_reference": r.reference,
                        "simulated": True,
                        "meta": _tag(r.meta, "B"),
                    }
                )
            )
        else:
            new_receipts.append(r)

    return replace(
        bundle, receipts=new_receipts, stats={**bundle.stats, "fault_b_rows": len(picked_ids)}
    )


def apply_fault_c(bundle: ImportBundle, rng: random.Random, count: int = 9) -> ImportBundle:
    """C: re-import duplicates a batch of receipts verbatim, new ids, same amount/date/ref.
    Only duplicates receipts with an intact reference untouched by an earlier fault (A/B
    would otherwise leave both copies with a blank/corrupted reference, so neither ever
    lands in the same R5 group and the duplicate fingerprint check never fires)."""
    candidates = sorted(
        (r for r in bundle.receipts if r.reference and "fault_id" not in r.meta),
        key=lambda r: r.id,
    )[:count]
    dupes = [
        r.model_copy(
            update={
                "id": f"{r.id}-dup",
                "simulated": True,
                "meta": _tag(r.meta, "C", duplicate_source_id=r.id),
            }
        )
        for r in candidates
    ]
    return replace(
        bundle,
        receipts=[*bundle.receipts, *dupes],
        stats={**bundle.stats, "fault_c_rows": len(dupes)},
    )


def apply_fault_d(
    bundle: ImportBundle,
    rng: random.Random,
    count: int = 6,
    pct_range: tuple[float, float] = (3.1, 3.5),
    r3_threshold: int = 500,
) -> ImportBundle:
    """D: one seller's receipts run short by 3.1-3.5% of total, each within the order's own
    shipping cost — just outside the R4 tolerance (2.9%) so it escalates as a cluster rather
    than a policy amendment target."""
    receivables_by_id = {rv.id: rv for rv in bundle.receivables}
    single_receipt_orders: dict[str, list[Receipt]] = defaultdict(list)
    for r in bundle.receipts:
        single_receipt_orders[r.reference].append(r)

    by_seller: dict[str, list[str]] = defaultdict(list)
    for receivable in bundle.receivables:
        if receivable.status != "open" or receivable.shipping <= 0:
            continue
        if len(single_receipt_orders.get(receivable.id, [])) != 1:
            continue
        for seller_id in receivable.meta.get("seller_ids", []):
            by_seller[seller_id].append(receivable.id)

    chosen: list[tuple[str, int]] = []
    for seller_id in sorted(by_seller):
        order_ids = sorted(by_seller[seller_id])
        if len(order_ids) < count:
            continue
        feasible: list[tuple[str, int]] = []
        for oid in order_ids:
            candidate = receivables_by_id[oid]
            pct = rng.uniform(*pct_range)
            shortfall = round(candidate.total * pct / 100)
            # Must also clear the R3 fixed threshold, or small orders get absorbed as
            # short_pay instead of escalating with the rest of the cluster.
            if r3_threshold < shortfall <= candidate.shipping:
                feasible.append((oid, shortfall))
            if len(feasible) == count:
                break
        if len(feasible) == count:
            chosen = feasible
            break

    if len(chosen) != count:
        raise AssertionError(
            f"fault D: could not find {count} eligible open orders for a single seller "
            f"(shortfall {pct_range[0]}-{pct_range[1]}% within shipping)"
        )

    shortfall_by_order = dict(chosen)
    touched_receipt_ids = set()
    for oid in shortfall_by_order:
        for r in single_receipt_orders[oid]:
            touched_receipt_ids.add(r.id)

    new_receipts = []
    for r in bundle.receipts:
        if r.id in touched_receipt_ids:
            shortfall = shortfall_by_order[r.reference]
            new_receipts.append(
                r.model_copy(
                    update={
                        "amount": r.amount - shortfall,
                        "simulated": True,
                        "meta": _tag(r.meta, "D", shortfall_centavos=shortfall),
                    }
                )
            )
        else:
            new_receipts.append(r)

    return replace(
        bundle,
        receipts=new_receipts,
        stats={**bundle.stats, "fault_d_rows": len(touched_receipt_ids)},
    )


D2_LOWER_PERCENT = 2.9
D2_UPPER_PERCENT = 3.6


def apply_fault_d2(bundle: ImportBundle, rng: random.Random, count: int = 2) -> ImportBundle:
    """D2: planted only if Milestone 1 finds fewer than 2 real rows a widened R4 (2.9 ->
    3.6%) would wrongly absorb. Meant to read as a genuine missing-item dispute rather
    than a freight variance -- in this slice no single line item's own price actually
    falls in the (2.9%, 3.6%] band (the smallest observed item share is ~6%, since most
    orders here have only 1-2 items), so the gap is a plain percentage like D's, on
    orders D didn't touch. The narrative point (a naive widening absorbs a non-freight
    gap) is what the checkpoint needs; it doesn't depend on sourcing the number from a
    real item row."""
    single_receipt_orders: dict[str, list[Receipt]] = defaultdict(list)
    for r in bundle.receipts:
        single_receipt_orders[r.reference].append(r)

    chosen: list[tuple[str, int]] = []
    for rv in sorted(bundle.receivables, key=lambda rv: rv.id):
        if rv.status != "open" or rv.shipping <= 0:
            continue
        rows = single_receipt_orders.get(rv.id, [])
        if len(rows) != 1 or "fault_id" in rows[0].meta:
            continue
        pct = rng.uniform(D2_LOWER_PERCENT, D2_UPPER_PERCENT)
        shortfall = round(rv.total * pct / 100)
        if 0 < shortfall <= rv.shipping:
            chosen.append((rv.id, shortfall))
        if len(chosen) == count:
            break

    if len(chosen) != count:
        raise AssertionError(
            f"fault D2: could not find {count} open orders with a shortfall inside "
            "(2.9%, 3.6%] that still fits within shipping"
        )

    shortfall_by_order = dict(chosen)
    touched_receipt_ids = {single_receipt_orders[oid][0].id for oid in shortfall_by_order}

    new_receipts = []
    for r in bundle.receipts:
        if r.id in touched_receipt_ids:
            shortfall = shortfall_by_order[r.reference]
            new_receipts.append(
                r.model_copy(
                    update={
                        "amount": r.amount - shortfall,
                        "simulated": True,
                        "meta": _tag(r.meta, "D2", shortfall_centavos=shortfall),
                    }
                )
            )
        else:
            new_receipts.append(r)

    return replace(
        bundle,
        receipts=new_receipts,
        stats={**bundle.stats, "fault_d2_rows": len(touched_receipt_ids)},
    )


def apply_fault_e(bundle: ImportBundle, rng: random.Random, count: int = 11) -> ImportBundle:
    """E: a payment processor nets card receipts by exactly 2.5%. By construction some are
    caught by R3/R4 as auto-write-offs (absorbed), some escalate — both must show up as one
    2.500%-ratio cluster once grouping looks at exceptions AND write-offs together."""
    receivables_by_id = {rv.id: rv for rv in bundle.receivables}
    receipts_per_reference: dict[str, int] = defaultdict(int)
    for r in bundle.receipts:
        receipts_per_reference[r.reference] += 1

    card_receipts = sorted(
        (
            r
            for r in bundle.receipts
            if r.meta.get("payment_type") == "credit_card"
            and "fault_id" not in r.meta
            and receipts_per_reference[r.reference] == 1
        ),
        key=lambda r: r.id,
    )

    escalate: list[tuple[Receipt, int]] = []
    absorb: list[tuple[Receipt, int]] = []
    for r in card_receipts:
        rv = receivables_by_id.get(r.reference)
        if rv is None or rv.total <= 0 or rv.status != "open":
            continue
        shortfall = round(rv.total * 0.025)
        if shortfall <= 0:
            continue
        would_escalate = shortfall > R3_THRESHOLD_CENTAVOS and shortfall > rv.shipping
        bucket = escalate if would_escalate else absorb
        if would_escalate and len(escalate) >= 4:
            continue
        if not would_escalate and len(absorb) >= (count - 4):
            continue
        bucket.append((r, shortfall))
        if len(escalate) == 4 and len(absorb) == count - 4:
            break

    chosen = (escalate + absorb)[:count]
    if len(chosen) != count:
        raise AssertionError(
            f"fault E: only found {len(chosen)}/{count} eligible credit_card receipts"
        )

    shortfall_by_id = {r.id: s for r, s in chosen}
    new_receipts = []
    for r in bundle.receipts:
        if r.id in shortfall_by_id:
            shortfall = shortfall_by_id[r.id]
            new_receipts.append(
                r.model_copy(
                    update={
                        "amount": r.amount - shortfall,
                        "simulated": True,
                        "meta": _tag(r.meta, "E", fee_pct=2.5, shortfall_centavos=shortfall),
                    }
                )
            )
        else:
            new_receipts.append(r)

    return replace(
        bundle,
        receipts=new_receipts,
        stats={**bundle.stats, "fault_e_rows": len(chosen), "fault_e_escalate": len(escalate)},
    )


def apply_fault_f(bundle: ImportBundle, rng: random.Random, count: int = 3) -> ImportBundle:
    """F: one bank transfer actually pays 2-3 open orders of the same customer, merged into
    a single receipt so no reference points at any one order."""
    receipts_by_order: dict[str, list[Receipt]] = defaultdict(list)
    for r in bundle.receipts:
        receipts_by_order[r.reference].append(r)
    open_by_customer: dict[str, list[Receivable]] = defaultdict(list)
    for rv in bundle.receivables:
        if rv.status == "open" and len(receipts_by_order.get(rv.id, [])) == 1:
            open_by_customer[rv.counterparty_id].append(rv)

    merges: list[tuple[str, list[Receivable]]] = []
    for cust_id in sorted(open_by_customer):
        orders = sorted(open_by_customer[cust_id], key=lambda rv: rv.id)
        if len(orders) < 2:
            continue
        group_size = 3 if len(orders) >= 3 and rng.random() < 0.5 else 2
        merges.append((cust_id, orders[:group_size]))
        if len(merges) == count:
            break

    if len(merges) != count:
        raise AssertionError(
            f"fault F: only found {len(merges)}/{count} customers with 2+ open orders"
        )

    merged_receipt_ids = set()
    new_receipts = []
    to_add = []
    for _cust_id, orders in merges:
        group_receipts = [receipts_by_order[rv.id][0] for rv in orders]
        merged_receipt_ids.update(r.id for r in group_receipts)
        total_amount = sum(r.amount for r in group_receipts)
        first = group_receipts[0]
        to_add.append(
            first.model_copy(
                update={
                    "id": f"{first.id}-merged",
                    "amount": total_amount,
                    "reference": orders[0].id,
                    "simulated": True,
                    "meta": _tag(first.meta, "F", merged_order_ids=[rv.id for rv in orders]),
                }
            )
        )

    for r in bundle.receipts:
        if r.id not in merged_receipt_ids:
            new_receipts.append(r)
    new_receipts.extend(to_add)

    return replace(
        bundle,
        receipts=new_receipts,
        stats={**bundle.stats, "fault_f_rows": len(to_add)},
    )


def apply_fault_g(bundle: ImportBundle, rng: random.Random, count: int = 5) -> ImportBundle:
    """G: the last instalment row is missing for 5 multi-row orders. Removed rows are
    stashed in bundle.holdback["G"] to arrive as "day 2" data."""
    by_order: dict[str, list[Receipt]] = defaultdict(list)
    for r in bundle.receipts:
        by_order[r.reference].append(r)

    candidates = [
        oid
        for oid, rows in sorted(by_order.items())
        if len(rows) > 1
        and any(row.meta.get("payment_installments", 1) > 1 for row in rows)
        and not any("fault_id" in row.meta for row in rows)
    ][:count]
    if len(candidates) != count:
        raise AssertionError(f"fault G: only found {len(candidates)}/{count} multi-row orders")

    removed = []
    removed_ids = set()
    for oid in candidates:
        last_row = max(by_order[oid], key=lambda r: r.meta.get("payment_sequential", 0))
        removed_ids.add(last_row.id)
        removed.append(
            last_row.model_copy(update={"simulated": True, "meta": _tag(last_row.meta, "G")})
        )

    new_receipts = [r for r in bundle.receipts if r.id not in removed_ids]
    holdback = {**bundle.holdback, "G": removed}
    return replace(
        bundle,
        receipts=new_receipts,
        holdback=holdback,
        stats={**bundle.stats, "fault_g_rows": len(removed)},
    )


FAULT_FUNCTIONS = {
    "A": apply_fault_a,
    "B": apply_fault_b,
    "C": apply_fault_c,
    "D": apply_fault_d,
    "D2": apply_fault_d2,
    "E": apply_fault_e,
    "F": apply_fault_f,
    "G": apply_fault_g,
}


def apply_all_faults(bundle: ImportBundle, manifest: list[dict], seed: int) -> ImportBundle:
    """Apply faults A-G in manifest order, one shared seeded RNG so the whole run is
    reproducible end to end."""
    rng = random.Random(seed)
    result = bundle
    for entry in manifest:
        fault_id = entry["id"]
        fn = FAULT_FUNCTIONS[fault_id]
        params = {k: v for k, v in entry.get("parameters", {}).items() if k != "seed"}
        result = fn(result, rng, **params)
    return result


def build_day2_bundle(
    day1_bundle: ImportBundle,
    fresh_bundle: ImportBundle,
    seed: int,
    new_a_count: int = 3,
    new_d_count: int = 2,
    new_d_pct: float = 3.4,
) -> ImportBundle:
    """Day 2 per spec 3.10: fresh receipts from the following month, plus fault G's
    withheld instalments (now arriving), plus a few new A- and D-pattern faults so
    recurrence watch has something to score: the alias fix should catch the new A rows
    (auto_handled), the tightened R4 bound should still escalate the new D rows at 3.4%
    (needed_human)."""
    rng = random.Random(seed)

    receipts = [*fresh_bundle.receipts, *day1_bundle.holdback.get("G", [])]
    bundle = replace(fresh_bundle, receipts=receipts)

    bundle = apply_fault_a(bundle, rng, count=new_a_count)
    bundle = apply_fault_d(bundle, rng, count=new_d_count, pct_range=(new_d_pct, new_d_pct))
    return bundle
