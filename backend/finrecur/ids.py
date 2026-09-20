"""Deterministic row ids. DB ids are derived from the source id (an Olist order id, a
customer_unique_id, or a fault-generated id) so a re-seed yields the same UUIDs. That is
what lets the committed Claude fixtures, keyed by input hash, replay after `make seed`."""

import uuid

_NAMESPACE = uuid.UUID("6f1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")


def stable_id(kind: str, source_id: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"{kind}:{source_id}")
