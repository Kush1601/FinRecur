"""Build the full current ledger state for a run's batch, by querying every
Allocation/Adjustment row for its receivables -- not just the ones a particular
service call happened to write. `finrecur.invariants.check` cross-checks totals
(e.g. no receipt allocated beyond its amount) against the WHOLE ledger, so a
partial, in-memory-only state understates it and can pass invariants that a full
query would fail. Shared by api/services/runs.py and api/services/fixes.py."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.models import Adjustment as AdjustmentModel
from api.models import Allocation as AllocationModel
from api.models import Receipt as ReceiptModel
from api.models import Receivable as ReceivableModel
from api.models import Run as RunModel
from finrecur.invariants import LedgerAdjustment, LedgerAllocation, LedgerReceipt, LedgerReceivable
from finrecur.invariants import LedgerState as LedgerState

BATCH_LINEAGE = {"day1": ("day1",), "day2": ("day1", "day2")}


def current_ledger_state(session: Session, run: RunModel, build_aliases) -> LedgerState:
    batches_included = list(BATCH_LINEAGE[run.batch_label])
    receivable_rows = session.scalars(
        select(ReceivableModel).where(ReceivableModel.meta["batch"].astext.in_(batches_included))
    ).all()
    receipt_rows = session.scalars(
        select(ReceiptModel).where(ReceiptModel.meta["batch"].astext.in_(batches_included))
    ).all()
    receivable_ids = [r.id for r in receivable_rows]
    allocations = (
        session.scalars(
            select(AllocationModel).where(AllocationModel.receivable_id.in_(receivable_ids))
        ).all()
        if receivable_ids
        else []
    )
    adjustments = (
        session.scalars(
            select(AdjustmentModel).where(AdjustmentModel.receivable_id.in_(receivable_ids))
        ).all()
        if receivable_ids
        else []
    )
    return LedgerState(
        receipts=[LedgerReceipt(str(r.id), str(r.counterparty_id), r.amount) for r in receipt_rows],
        receivables=[
            LedgerReceivable(
                str(r.id), str(r.counterparty_id), r.total, r.allocated, r.adjusted, r.remaining
            )
            for r in receivable_rows
        ],
        allocations=[
            LedgerAllocation(str(a.receipt_id), str(a.receivable_id), a.amount) for a in allocations
        ],
        adjustments=[
            LedgerAdjustment(str(a.receivable_id), a.amount, a.reason.value) for a in adjustments
        ],
        aliases=build_aliases(session),
    )
