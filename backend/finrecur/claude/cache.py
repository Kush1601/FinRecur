"""Cache Claude calls by input hash so CI never needs a key. Committed under
backend/tests/fixtures/claude/ -- spec 3.9: "Claude responses cached by input hash."
"""

import hashlib
import json
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "claude"


def cache_key(model: str, system: str, tool_schema: dict, input_payload: dict) -> str:
    payload = json.dumps(
        {"model": model, "system": system, "tool_schema": tool_schema, "input": input_payload},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def read_cached(key: str) -> dict | None:
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def write_cache(key: str, value: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{key}.json").write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
