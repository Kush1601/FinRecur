"""Seed the demo: fetch Olist if missing, import a slice, apply data/faults.json in demo
mode, write the committed slice CSVs, load Postgres, seed PolicyVersion v1. Re-runnable --
each run truncates and reloads.

Run with `cd backend && uv run python ../scripts/seed.py`.
"""

import csv
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from finrecur.faults import apply_all_faults, build_day2_bundle  # noqa: E402
from finrecur.ids import stable_id  # noqa: E402
from finrecur.importers import ImportBundle  # noqa: E402
from finrecur.importers.olist import OlistImporter, pick_default_month  # noqa: E402

RAW_DIR = REPO_ROOT / "data" / "olist" / "raw"
SLICE_DIR = REPO_ROOT / "data" / "olist" / "slice"
FAULTS_MANIFEST_PATH = REPO_ROOT / "data" / "faults.json"
MAX_ORDERS = 1200
FAULT_SEED = 1601


def _next_month(month: str) -> str:
    year, mon = (int(x) for x in month.split("-"))
    year, mon = (year + 1, 1) if mon == 12 else (year, mon + 1)
    return f"{year:04d}-{mon:02d}"


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _write_bundle_csvs(bundle: ImportBundle, out_dir: Path) -> None:
    _write_csv(
        out_dir / "counterparties.csv",
        [
            {
                "id": c.id,
                "external_id": c.external_id,
                "name": c.name,
                "simulated": c.simulated,
                "meta": json.dumps(c.meta),
            }
            for c in bundle.counterparties
        ],
    )
    _write_csv(
        out_dir / "receivables.csv",
        [
            {
                "id": r.id,
                "counterparty_id": r.counterparty_id,
                "total": r.total,
                "shipping": r.shipping,
                "issued_at": r.issued_at.isoformat(),
                "status": r.status,
                "source": r.source,
                "simulated": r.simulated,
                "meta": json.dumps(r.meta),
            }
            for r in bundle.receivables
        ],
    )
    _write_csv(
        out_dir / "receipts.csv",
        [
            {
                "id": r.id,
                "counterparty_id": r.counterparty_id,
                "amount": r.amount,
                "received_at": r.received_at.isoformat(),
                "reference": r.reference,
                "original_reference": r.original_reference or "",
                "memo": r.memo or "",
                "simulated": r.simulated,
                "duplicate_of": r.duplicate_of or "",
                "meta": json.dumps(r.meta),
            }
            for r in bundle.receipts
        ],
    )


def _build_day1(month: str) -> ImportBundle:
    from api.settings import settings

    bundle = OlistImporter(RAW_DIR, month=month, max_orders=MAX_ORDERS).load()
    if settings.FINRECUR_DEMO:
        manifest = json.loads(FAULTS_MANIFEST_PATH.read_text())["faults"]
        bundle = apply_all_faults(bundle, manifest, seed=FAULT_SEED)
    return bundle


def _build_day2(day1_bundle: ImportBundle, month: str) -> ImportBundle:
    fresh_month = _next_month(month)
    fresh = OlistImporter(RAW_DIR, month=fresh_month, max_orders=200).load()
    return build_day2_bundle(day1_bundle, fresh, seed=FAULT_SEED)


def _load_postgres(day1: ImportBundle) -> dict:
    """Loads day 1 only. Day 2 stays on disk as CSVs under data/olist/slice/day2/ and is
    appended into Postgres at run time by api/services/batches.py, tagged meta.batch="day2" --
    this is what lets POST /runs {batch: "day2"} demonstrate an incremental batch."""
    from sqlalchemy import text

    from api.db import SessionLocal
    from api.models import Base, PolicyVersion
    from api.models import Counterparty as CounterpartyModel
    from api.models import Receipt as ReceiptModel
    from api.models import Receivable as ReceivableModel

    now = datetime.now(UTC)
    all_counterparties = {c.id: c for c in day1.counterparties}
    all_receivables = day1.receivables
    all_receipts = day1.receipts

    with SessionLocal() as db:
        # Everything downstream of the source rows (runs, decisions, clusters, fixes,
        # ledger) goes too: a seed is a fresh demo, and the FKs would refuse otherwise.
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        db.execute(text(f"TRUNCATE TABLE {tables} CASCADE"))

        cp_id_map: dict[str, uuid.UUID] = {}
        for c in all_counterparties.values():
            row = CounterpartyModel(
                id=stable_id("counterparty", c.external_id),
                external_id=c.external_id,
                name=c.name,
                simulated=c.simulated,
                meta=c.meta,
                created_at=now,
            )
            db.add(row)
            cp_id_map[c.id] = row.id

        # receivable.id / receipt.id from the importer are Olist order ids (or fault-
        # generated ids), not DB uuids. Keep them in meta.source_id so a rule engine can
        # still match a receipt's `reference` (an order id) against a receivable.
        for r in all_receivables:
            db.add(
                ReceivableModel(
                    id=stable_id("receivable", r.id),
                    counterparty_id=cp_id_map[r.counterparty_id],
                    total=r.total,
                    shipping=r.shipping,
                    allocated=0,
                    adjusted=0,
                    remaining=r.total,
                    status=r.status,
                    issued_at=r.issued_at,
                    source=r.source,
                    simulated=r.simulated,
                    meta={**r.meta, "source_id": r.id, "batch": "day1"},
                )
            )

        for r in all_receipts:
            db.add(
                ReceiptModel(
                    id=stable_id("receipt", r.id),
                    counterparty_id=cp_id_map[r.counterparty_id],
                    amount=r.amount,
                    received_at=r.received_at,
                    reference=r.reference,
                    original_reference=r.original_reference,
                    memo=r.memo,
                    simulated=r.simulated,
                    meta={**r.meta, "source_id": r.id, "batch": "day1"},
                )
            )

        db.add(
            PolicyVersion(
                version=1,
                created_by="seed",
                created_at=now,
                rules={
                    "R5": {
                        "kind": "group",
                        "source": "standard AR practice -- group payment rows by cited "
                        "order id, sum == total applies as one",
                    },
                    "R1": {
                        "kind": "exact",
                        "source": "Exact cited-order match",
                    },
                    "R2": {
                        "kind": "window_days",
                        "window_days": 30,
                        "source": "Amount and counterparty match within the configured date window",
                    },
                    "R7": {
                        "kind": "tolerance",
                        "tolerance_centavos": 1,
                        "source": "standard AR practice",
                    },
                    "R3": {
                        "kind": "threshold",
                        "threshold_centavos": 500,
                        "source": "demo policy",
                    },
                    "R4": {
                        "kind": "percent",
                        "max_percent": 2.9,
                        "must_fit_shipping": True,
                        "source": "demo policy",
                    },
                    "R6": {
                        "kind": "overpayment",
                        "source": "standard AR practice",
                    },
                    "R8": {
                        "kind": "escalate",
                        "source": "Fallback when no supported rule can settle the receipt",
                    },
                },
            )
        )
        db.commit()

    return {
        "counterparties": len(all_counterparties),
        "receivables": len(all_receivables),
        "receipts": len(all_receipts),
    }


def main() -> None:
    if not RAW_DIR.exists() or not any(RAW_DIR.iterdir()):
        print("Olist raw data missing, fetching...")
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import fetch_olist

        fetch_olist.main()

    month = pick_default_month(RAW_DIR)
    print(f"using month {month} (most orders in 2017), capped at {MAX_ORDERS} orders")

    day1 = _build_day1(month)
    day2 = _build_day2(day1, month)

    _write_bundle_csvs(day1, SLICE_DIR / "day1")
    _write_bundle_csvs(day2, SLICE_DIR / "day2")

    counts = _load_postgres(day1)

    print(f"day 1: {len(day1.receivables)} receivables, {len(day1.receipts)} receipts")
    print(f"       fault rows: {[(k, v) for k, v in day1.stats.items() if k.startswith('fault_')]}")
    print(f"day 2: {len(day2.receivables)} receivables, {len(day2.receipts)} receipts")
    print(f"       holdback (fault G) rows carried in: {len(day1.holdback.get('G', []))}")
    print(f"loaded into postgres: {counts}")


if __name__ == "__main__":
    main()
