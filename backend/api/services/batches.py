"""Loads a batch's source rows into Postgres before a run. Day 1 rows are already
loaded by scripts/seed.py (tagged meta.batch="day1"); day 2 appends
data/olist/slice/day2/*.csv the first time it's requested, tagged meta.batch="day2",
so a repeated POST /runs {batch: "day2"} doesn't reload it."""

import csv
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.models import Counterparty, Receipt, Receivable
from finrecur.ids import stable_id

REPO_ROOT = Path(__file__).resolve().parents[3]
DAY2_DIR = REPO_ROOT / "data" / "olist" / "slice" / "day2"

KNOWN_BATCHES = ("day1", "day2")


def _parse_bool(s: str) -> bool:
    return s.strip().lower() == "true"


def ensure_batch_loaded(session: Session, batch: str) -> None:
    if batch not in KNOWN_BATCHES:
        raise ValueError(f"unknown batch: {batch!r}")
    if batch == "day1":
        return
    already = session.execute(
        select(Receivable.id).where(Receivable.meta["batch"].astext == "day2").limit(1)
    ).first()
    if already is not None:
        return
    _load_day2(session)


def _read_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _load_day2(session: Session) -> None:
    counterparty_rows = _read_rows(DAY2_DIR / "counterparties.csv")
    receivable_rows = _read_rows(DAY2_DIR / "receivables.csv")
    receipt_rows = _read_rows(DAY2_DIR / "receipts.csv")
    now = datetime.now(UTC)

    existing_by_external_id = {
        c.external_id: c.id for c in session.scalars(select(Counterparty)).all()
    }
    # ImportBundle counterparty ids are their external ids. Day 2 also contains
    # fault G's withheld Day 1 receipts, whose counterparties are already in the
    # database but are not repeated in day2/counterparties.csv.
    cp_id_map: dict[str, uuid.UUID] = dict(existing_by_external_id)
    for row in counterparty_rows:
        external_id = row["external_id"]
        if external_id in existing_by_external_id:
            cp_id_map[row["id"]] = existing_by_external_id[external_id]
            continue
        cp = Counterparty(
            id=stable_id("counterparty", external_id),
            external_id=external_id,
            name=row["name"],
            simulated=_parse_bool(row["simulated"]),
            meta=json.loads(row["meta"]),
            created_at=now,
        )
        session.add(cp)
        cp_id_map[row["id"]] = cp.id
        existing_by_external_id[external_id] = cp.id

    for row in receivable_rows:
        meta = {**json.loads(row["meta"]), "source_id": row["id"], "batch": "day2"}
        total = int(row["total"])
        session.add(
            Receivable(
                id=stable_id("receivable", row["id"]),
                counterparty_id=cp_id_map[row["counterparty_id"]],
                total=total,
                shipping=int(row["shipping"]),
                allocated=0,
                adjusted=0,
                remaining=total,
                status=row["status"],
                issued_at=datetime.fromisoformat(row["issued_at"]),
                source=row["source"],
                simulated=_parse_bool(row["simulated"]),
                meta=meta,
            )
        )

    for row in receipt_rows:
        meta = {**json.loads(row["meta"]), "source_id": row["id"], "batch": "day2"}
        session.add(
            Receipt(
                id=stable_id("receipt", row["id"]),
                counterparty_id=cp_id_map[row["counterparty_id"]],
                amount=int(row["amount"]),
                received_at=datetime.fromisoformat(row["received_at"]),
                reference=row["reference"],
                original_reference=row["original_reference"] or None,
                memo=row["memo"] or None,
                simulated=_parse_bool(row["simulated"]),
                meta=meta,
            )
        )

    session.commit()
