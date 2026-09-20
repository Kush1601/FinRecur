"""Download the Olist CSVs used by the importer into data/olist/raw/ (gitignored).
Idempotent: skips any file already present. Run with `uv run python scripts/fetch_olist.py`
from backend/, or `python3 scripts/fetch_olist.py` from the repo root.
"""

import sys
import urllib.request
from pathlib import Path

BASE_URL = (
    "https://raw.githubusercontent.com/JoaquinMenendezz/"
    "Analise-de-Dados-work_at_olist_data/HEAD/dados/"
)

FILES = [
    "olist_orders_dataset.csv",
    "olist_order_items_dataset.csv",
    "olist_order_payments_dataset.csv",
    "olist_customers_dataset.csv",
    "olist_sellers_dataset.csv",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "olist" / "raw"

ATTRIBUTION = """# Olist dataset attribution

Data by Olist ("Brazilian E-Commerce Public Dataset by Olist", via Kaggle:
https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce), licensed CC BY-NC-SA 4.0.

Used here non-commercially, for a portfolio demo (FinRecur). Not redistributed in full: a
one-month slice is committed under `data/olist/slice/` for zero-setup clone-and-run; the
full raw CSVs are fetched on demand by `scripts/fetch_olist.py` and are gitignored.
"""


def fetch_file(name: str) -> None:
    dest = RAW_DIR / name
    if dest.exists():
        print(f"skip  {name} (already present)")
        return
    url = BASE_URL + name
    print(f"fetch {name} ...", end=" ", flush=True)
    urllib.request.urlretrieve(url, dest)
    size_kb = dest.stat().st_size / 1024
    print(f"done ({size_kb:.0f} KB)")


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        fetch_file(name)
    attribution_path = REPO_ROOT / "data" / "olist" / "ATTRIBUTION.md"
    attribution_path.write_text(ATTRIBUTION)
    print(f"wrote {attribution_path}")


if __name__ == "__main__":
    sys.exit(main())
