"""Olist order data -> canonical ImportBundle. Identity is customer_unique_id, never
customer_id (spec 3.4 review fix 2) — Olist gives one customer_id per order even for a
repeat shopper, customer_unique_id is the real counterparty."""

import csv
import datetime as dt
from collections import defaultdict
from pathlib import Path

from finrecur.importers import ImportBundle
from finrecur.money import from_float
from finrecur.schema import Counterparty, Receipt, Receivable

CANCELLED_STATUSES = {"canceled", "unavailable"}


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.UTC)


def month_order_counts(raw_dir: Path) -> dict[str, int]:
    """Order counts per YYYY-MM, for picking a default month."""
    counts: dict[str, int] = defaultdict(int)
    for row in _read_csv(raw_dir / "olist_orders_dataset.csv"):
        counts[row["order_purchase_timestamp"][:7]] += 1
    return dict(counts)


def pick_default_month(raw_dir: Path, year_prefix: str = "2017") -> str:
    """The month with the most orders in `year_prefix`, per the build spec."""
    counts = month_order_counts(raw_dir)
    candidates = {m: n for m, n in counts.items() if m.startswith(year_prefix)}
    if not candidates:
        candidates = counts
    return max(candidates, key=lambda m: candidates[m])


class OlistImporter:
    def __init__(self, raw_dir: Path, month: str, max_orders: int | None = None):
        self.raw_dir = Path(raw_dir)
        self.month = month
        self.max_orders = max_orders

    def load(self) -> ImportBundle:
        customers = {
            row["customer_id"]: row
            for row in _read_csv(self.raw_dir / "olist_customers_dataset.csv")
        }
        sellers = {
            row["seller_id"]: row for row in _read_csv(self.raw_dir / "olist_sellers_dataset.csv")
        }

        orders = [
            row
            for row in _read_csv(self.raw_dir / "olist_orders_dataset.csv")
            if row["order_purchase_timestamp"].startswith(self.month)
        ]
        orders.sort(key=lambda r: r["order_purchase_timestamp"])
        if self.max_orders is not None:
            orders = orders[: self.max_orders]
        order_ids = {row["order_id"] for row in orders}

        items_by_order: dict[str, list[dict]] = defaultdict(list)
        for row in _read_csv(self.raw_dir / "olist_order_items_dataset.csv"):
            if row["order_id"] in order_ids:
                items_by_order[row["order_id"]].append(row)

        payments_by_order: dict[str, list[dict]] = defaultdict(list)
        for row in _read_csv(self.raw_dir / "olist_order_payments_dataset.csv"):
            if row["order_id"] in order_ids:
                payments_by_order[row["order_id"]].append(row)

        seen_counterparties: dict[str, Counterparty] = {}
        receivables: list[Receivable] = []
        receipts: list[Receipt] = []
        stats = {"orders": 0, "skipped_zero_payments": 0, "receipts": 0}

        for order in orders:
            customer = customers[order["customer_id"]]
            external_id = customer["customer_unique_id"]
            if external_id not in seen_counterparties:
                seen_counterparties[external_id] = Counterparty(
                    id=external_id,
                    external_id=external_id,
                    name=f"Customer {external_id[:6]}",
                    simulated=True,
                    meta={"generated_name": True},
                )

            items = items_by_order.get(order["order_id"], [])
            price_total = sum(float(i["price"]) for i in items)
            freight_total = sum(float(i["freight_value"]) for i in items)
            seller_ids = sorted({i["seller_id"] for i in items})
            seller_locations = [
                {
                    "seller_id": seller_id,
                    "city": sellers[seller_id]["seller_city"],
                    "state": sellers[seller_id]["seller_state"],
                }
                for seller_id in seller_ids
                if seller_id in sellers
            ]
            item_prices = [from_float(float(i["price"])) for i in items]

            status = "cancelled" if order["order_status"] in CANCELLED_STATUSES else "open"
            issued_at = _parse_ts(order["order_purchase_timestamp"])

            receivables.append(
                Receivable(
                    id=order["order_id"],
                    counterparty_id=external_id,
                    total=from_float(price_total + freight_total),
                    shipping=from_float(freight_total),
                    issued_at=issued_at,
                    status=status,
                    source="olist",
                    simulated=False,
                    meta={
                        "seller_ids": seller_ids,
                        "seller_locations": seller_locations,
                        "order_status": order["order_status"],
                        "customer_state": customer["customer_state"],
                        "item_prices": item_prices,
                    },
                )
            )
            stats["orders"] += 1

            for payment in payments_by_order.get(order["order_id"], []):
                value = float(payment["payment_value"])
                if value == 0:
                    stats["skipped_zero_payments"] += 1
                    continue

                approved_at = order["order_approved_at"]
                meta: dict = {
                    "payment_type": payment["payment_type"],
                    "payment_sequential": int(payment["payment_sequential"]),
                    "payment_installments": int(payment["payment_installments"]),
                    "customer_state": customer["customer_state"],
                    "seller_locations": seller_locations,
                }
                simulated_date = not approved_at
                received_at = _parse_ts(approved_at) if approved_at else issued_at
                if simulated_date:
                    meta["simulated_date"] = True

                receipts.append(
                    Receipt(
                        id=f"{order['order_id']}-p{payment['payment_sequential']}",
                        counterparty_id=external_id,
                        amount=from_float(value),
                        received_at=received_at,
                        reference=order["order_id"],
                        # The UI's "simulated" tag reads this field, not meta --
                        # a fabricated received_at must be flagged here too.
                        simulated=simulated_date,
                        meta=meta,
                    )
                )
                stats["receipts"] += 1

        return ImportBundle(
            counterparties=list(seen_counterparties.values()),
            receivables=receivables,
            receipts=receipts,
            stats=stats,
        )
