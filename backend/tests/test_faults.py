import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from finrecur.faults import (
    FAULT_FUNCTIONS,
    apply_all_faults,
    apply_fault_a,
    apply_fault_b,
    apply_fault_c,
    apply_fault_d,
    apply_fault_e,
    apply_fault_f,
    apply_fault_g,
)
from finrecur.importers import ImportBundle
from finrecur.importers.olist import OlistImporter
from finrecur.schema import Counterparty, Receipt, Receivable

WHEN = datetime(2017, 11, 1, tzinfo=UTC)


def cp(i: str) -> Counterparty:
    return Counterparty(id=i, external_id=i, name=f"Customer {i}")


def rv(
    i: str,
    cust: str,
    total: int,
    shipping: int = 0,
    status: Literal["open", "partially_paid", "settled", "cancelled"] = "open",
    **meta,
) -> Receivable:
    return Receivable(
        id=i,
        counterparty_id=cust,
        total=total,
        shipping=shipping,
        issued_at=WHEN,
        status=status,
        meta=meta,
    )


def rc(i: str, cust: str, amount: int, reference: str, **meta) -> Receipt:
    return Receipt(
        id=i, counterparty_id=cust, amount=amount, received_at=WHEN, reference=reference, meta=meta
    )


def rng():
    return random.Random(1601)


class TestFaultA:
    def test_moves_receipts_to_new_variant_counterparty(self):
        receipts = [rc(f"r{i}", "cust1", 1000, f"order{i}") for i in range(5)]
        bundle = ImportBundle(counterparties=[cp("cust1")], receivables=[], receipts=receipts)

        out = apply_fault_a(bundle, rng(), count=3)

        variant = next(c for c in out.counterparties if c.id != "cust1")
        assert variant.simulated
        assert variant.meta["fault_id"] == "A"
        moved = [r for r in out.receipts if r.counterparty_id == variant.id]
        assert len(moved) == 3
        for r in moved:
            assert r.reference == ""
            assert r.simulated
            assert r.meta["fault_id"] == "A"
        untouched = [r for r in out.receipts if r.counterparty_id == "cust1"]
        assert len(untouched) == 2


class TestFaultB:
    def test_corrupts_reference_keeps_original(self):
        receipts = [rc(f"r{i}", "cust1", 1000, f"order{i:04d}") for i in range(4)]
        bundle = ImportBundle(counterparties=[], receivables=[], receipts=receipts)

        out = apply_fault_b(bundle, rng(), count=2)

        touched = [r for r in out.receipts if r.simulated]
        assert len(touched) == 2
        for r in touched:
            assert r.original_reference is not None
            assert r.reference != r.original_reference
            assert r.meta["fault_id"] == "B"


class TestFaultC:
    def test_duplicates_rows_verbatim(self):
        receipts = [rc(f"r{i}", "cust1", 1000, f"order{i}") for i in range(3)]
        bundle = ImportBundle(counterparties=[], receivables=[], receipts=receipts)

        out = apply_fault_c(bundle, rng(), count=2)

        assert len(out.receipts) == 5
        dupes = [r for r in out.receipts if r.simulated]
        assert len(dupes) == 2
        for dupe in dupes:
            source = next(r for r in receipts if r.id == dupe.meta["duplicate_source_id"])
            assert dupe.amount == source.amount
            assert dupe.reference == source.reference
            assert dupe.id != source.id
            assert dupe.meta["fault_id"] == "C"


class TestFaultD:
    def test_shorts_one_sellers_receipts_within_shipping(self):
        receivables = []
        receipts = []
        for i in range(6):
            oid = f"o{i}"
            receivables.append(
                rv(oid, "cust1", total=300_00, shipping=15_00, seller_ids=["seller1"])
            )
            receipts.append(rc(f"r{i}", "cust1", 100_00, oid))
        bundle = ImportBundle(counterparties=[], receivables=receivables, receipts=receipts)

        out = apply_fault_d(bundle, rng(), count=6, pct_range=(3.1, 3.5))

        touched = [r for r in out.receipts if r.simulated]
        assert len(touched) == 6
        for r in touched:
            shortfall = r.meta["shortfall_centavos"]
            assert 500 < shortfall <= 15_00
            assert r.amount == 100_00 - shortfall
            assert r.meta["fault_id"] == "D"

    def test_raises_when_no_seller_has_enough_orders(self):
        receivables = [rv("o1", "cust1", total=100_00, shipping=10_00, seller_ids=["seller1"])]
        receipts = [rc("r1", "cust1", 100_00, "o1")]
        bundle = ImportBundle(counterparties=[], receivables=receivables, receipts=receipts)

        with pytest.raises(AssertionError):
            apply_fault_d(bundle, rng(), count=6)


class TestFaultE:
    def test_splits_escalated_and_absorbed(self):
        receivables = []
        receipts = []
        # 4 that should escalate: 2.5% shortfall > R$5 and > shipping
        for i in range(4):
            oid = f"big{i}"
            receivables.append(rv(oid, "cust1", total=100_000, shipping=100))
            receipts.append(rc(f"rbig{i}", "cust1", 100_000, oid, payment_type="credit_card"))
        # 7 that should be absorbed: shortfall <= R$5
        for i in range(7):
            oid = f"small{i}"
            receivables.append(rv(oid, "cust1", total=10_000, shipping=0))
            receipts.append(rc(f"rsmall{i}", "cust1", 10_000, oid, payment_type="credit_card"))
        bundle = ImportBundle(counterparties=[], receivables=receivables, receipts=receipts)

        out = apply_fault_e(bundle, rng(), count=11)

        touched = [r for r in out.receipts if r.simulated]
        assert len(touched) == 11
        assert out.stats["fault_e_escalate"] == 4
        for r in touched:
            assert r.meta["fault_id"] == "E"
            assert r.meta["fee_pct"] == 2.5


class TestFaultF:
    def test_merges_open_orders_of_one_customer(self):
        receivables = [rv(f"o{i}", "cust1", total=50_00) for i in range(2)]
        receipts = [rc(f"r{i}", "cust1", 50_00, f"o{i}") for i in range(2)]
        bundle = ImportBundle(counterparties=[], receivables=receivables, receipts=receipts)

        out = apply_fault_f(bundle, rng(), count=1)

        assert len(out.receipts) == 1
        merged = out.receipts[0]
        assert merged.amount == 100_00
        assert merged.reference == "o0"
        assert merged.simulated
        assert merged.meta["fault_id"] == "F"
        assert set(merged.meta["merged_order_ids"]) == {"o0", "o1"}


class TestFaultG:
    def test_removes_last_instalment_row(self):
        receipts = [
            rc("r1a", "cust1", 50_00, "o1", payment_sequential=1, payment_installments=2),
            rc("r1b", "cust1", 50_00, "o1", payment_sequential=2, payment_installments=2),
        ]
        bundle = ImportBundle(counterparties=[], receivables=[], receipts=receipts)

        out = apply_fault_g(bundle, rng(), count=1)

        assert len(out.receipts) == 1
        assert out.receipts[0].id == "r1a"
        assert len(out.holdback["G"]) == 1
        removed = out.holdback["G"][0]
        assert removed.id == "r1b"
        assert removed.simulated
        assert removed.meta["fault_id"] == "G"


def test_all_fault_functions_registered():
    assert set(FAULT_FUNCTIONS) == {"A", "B", "C", "D", "D2", "E", "F", "G"}


class TestAllFaultsOnRealSlice:
    def test_no_untagged_modified_rows(self, repo_root: Path, faults_manifest: list[dict]):
        raw_dir = repo_root / "data" / "olist" / "raw"
        if not raw_dir.exists() or not any(raw_dir.iterdir()):
            pytest.skip("Olist raw data not fetched; run scripts/fetch_olist.py first")

        importer = OlistImporter(raw_dir, month="2017-11", max_orders=1200)
        bundle = importer.load()
        before_by_id = {r.id: r for r in bundle.receipts}

        out = apply_all_faults(bundle, faults_manifest, seed=1601)

        for r in out.receipts:
            before = before_by_id.get(r.id)
            if before is None:
                # a row that didn't exist before this fault run (duplicate, merge) must be tagged
                assert r.simulated and "fault_id" in r.meta
                continue
            if r != before:
                assert r.simulated, f"receipt {r.id} changed but is not simulated"
                assert "fault_id" in r.meta, f"receipt {r.id} changed but has no fault_id"
