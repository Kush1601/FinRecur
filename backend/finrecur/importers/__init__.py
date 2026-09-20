"""Importer interface: source data -> canonical ImportBundle. The rule engine
never sees a source format, only Counterparty/Receivable/Receipt rows."""

from dataclasses import dataclass, field
from typing import Any, Protocol

from finrecur.schema import Counterparty, Receipt, Receivable


@dataclass
class ImportBundle:
    counterparties: list[Counterparty] = field(default_factory=list)
    receivables: list[Receivable] = field(default_factory=list)
    receipts: list[Receipt] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    holdback: dict[str, Any] = field(default_factory=dict)


class Importer(Protocol):
    def load(self) -> ImportBundle: ...
