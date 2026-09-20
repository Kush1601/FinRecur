"""Literal fixtures per rule, order R5 -> R1 -> R2 -> R7 -> R3 -> R4 -> R6 -> R8."""

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from finrecur.rules.engine import run_rules
from finrecur.rules.types import ReasonCode
from finrecur.schema import Receipt, Receivable

POLICY = {
    "R2": {"window_days": 30},
    "R7": {"tolerance_centavos": 1},
    "R3": {"threshold_centavos": 500},
    "R4": {"max_percent": 2.9, "must_fit_shipping": True},
}


def dt(offset_days: int) -> datetime:
    return datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=offset_days)


ReceivableStatus = Literal["open", "partially_paid", "settled", "cancelled"]


def receivable(
    id: str, total: int, shipping: int = 0, status: ReceivableStatus = "open", **kw
) -> Receivable:
    return Receivable(
        id=id,
        counterparty_id=kw.pop("counterparty_id", "cp-1"),
        total=total,
        shipping=shipping,
        issued_at=kw.pop("issued_at", dt(1)),
        status=status,
        **kw,
    )


def receipt(id: str, amount: int, reference: str, **kw) -> Receipt:
    return Receipt(
        id=id,
        counterparty_id=kw.pop("counterparty_id", "cp-1"),
        amount=amount,
        received_at=kw.pop("received_at", dt(2)),
        reference=reference,
        **kw,
    )


def decisions_by_id(receivables, receipts, aliases=None):
    ds = run_rules(receivables, receipts, aliases or {}, POLICY)
    return {d.receipt_id: d for d in ds}


# --- R5: group sum ---------------------------------------------------------------


def test_r5_group_sum_matches():
    rv = receivable("rv-1", total=1000)
    r1 = receipt("r-1", 400, "rv-1", received_at=dt(2))
    r2 = receipt("r-2", 600, "rv-1", received_at=dt(3))
    ds = decisions_by_id([rv], [r1, r2])
    assert ds["r-1"].outcome == "matched"
    assert ds["r-1"].rule_id == "R5"
    assert ds["r-1"].matched_by == "group"
    assert ds["r-2"].rule_id == "R5"
    assert ds["r-1"].allocations == [("rv-1", 400)]
    assert ds["r-2"].allocations == [("rv-1", 600)]


# --- R1: single exact match, cancelled receivable ---------------------------------


def test_r1_exact_match():
    rv = receivable("rv-1", total=1000)
    r1 = receipt("r-1", 1000, "rv-1")
    ds = decisions_by_id([rv], [r1])
    assert ds["r-1"].outcome == "matched"
    assert ds["r-1"].rule_id == "R1"
    assert ds["r-1"].matched_by == "exact"


def test_exact_ref_on_cancelled_receivable_escalates():
    rv = receivable("rv-1", total=1000, status="cancelled")
    r1 = receipt("r-1", 1000, "rv-1")
    ds = decisions_by_id([rv], [r1])
    assert ds["r-1"].outcome == "escalated"
    assert ds["r-1"].reason_code == ReasonCode.PAYMENT_ON_CANCELLED


# --- R2: amount + date window --------------------------------------------------


def test_r2_exactly_one_candidate_matches():
    rv = receivable("rv-1", total=500, issued_at=dt(1))
    r1 = receipt("r-1", 500, "", received_at=dt(10))
    ds = decisions_by_id([rv], [r1])
    assert ds["r-1"].outcome == "matched"
    assert ds["r-1"].rule_id == "R2"
    assert ds["r-1"].matched_by == "amount_date"


def test_r2_two_candidates_escalates_multiple():
    rv1 = receivable("rv-1", total=500, issued_at=dt(1))
    rv2 = receivable("rv-2", total=500, issued_at=dt(2))
    r1 = receipt("r-1", 500, "", received_at=dt(10))
    ds = decisions_by_id([rv1, rv2], [r1])
    assert ds["r-1"].outcome == "escalated"
    assert ds["r-1"].reason_code == ReasonCode.MULTIPLE_CANDIDATES


def test_r2_wrong_customer_excluded_no_match():
    rv = receivable("rv-1", total=500, issued_at=dt(1), counterparty_id="cp-other")
    r1 = receipt("r-1", 500, "", received_at=dt(10), counterparty_id="cp-1")
    ds = decisions_by_id([rv], [r1])
    assert ds["r-1"].outcome == "escalated"
    assert ds["r-1"].reason_code == ReasonCode.NO_MATCH


def test_r2_window_edge():
    # window_days=30: issued day 1, received day 31 -> exactly 30 days -> matches.
    rv = receivable("rv-1", total=500, issued_at=dt(1))
    r1 = receipt("r-1", 500, "", received_at=dt(31))
    ds = decisions_by_id([rv], [r1])
    assert ds["r-1"].outcome == "matched"

    # one day later (31 days) -> outside the window -> no_match.
    rv2 = receivable("rv-2", total=500, issued_at=dt(1))
    r2 = receipt("r-2", 500, "", received_at=dt(32))
    ds2 = decisions_by_id([rv2], [r2])
    assert ds2["r-2"].outcome == "escalated"
    assert ds2["r-2"].reason_code == ReasonCode.NO_MATCH


# --- R7: one-centavo rounding ---------------------------------------------------


def test_r7_one_centavo_shortfall_matches_rounding():
    rv = receivable("rv-1", total=1000)
    r1 = receipt("r-1", 999, "rv-1")
    ds = decisions_by_id([rv], [r1])
    assert ds["r-1"].outcome == "matched"
    assert ds["r-1"].rule_id == "R7"
    assert ds["r-1"].adjustments == [("rv-1", 1, "rounding")]


# --- R3: short-pay threshold at the boundary ------------------------------------


def test_r3_at_threshold_matches():
    rv = receivable("rv-1", total=10000, shipping=0)
    r1 = receipt("r-1", 9500, "rv-1")  # shortfall 500 == threshold
    ds = decisions_by_id([rv], [r1])
    assert ds["r-1"].outcome == "matched"
    assert ds["r-1"].rule_id == "R3"
    assert ds["r-1"].adjustments == [("rv-1", 500, "short_pay")]


def test_r3_one_centavo_over_threshold_escalates():
    rv = receivable("rv-1", total=10000, shipping=0)
    r1 = receipt("r-1", 9499, "rv-1")  # shortfall 501, and 501 > 2.9% of 10000 (290)
    ds = decisions_by_id([rv], [r1])
    assert ds["r-1"].outcome == "escalated"
    assert ds["r-1"].reason_code == ReasonCode.SHORT_PAY
    refused = {t["rule"] for t in ds["r-1"].evidence["rules_tried"]}
    assert refused == {"R7", "R3", "R4"}


# --- R4: percent + shipping boundary --------------------------------------------


def test_r4_at_percent_boundary_matches():
    rv = receivable("rv-1", total=20000, shipping=1000)
    r1 = receipt("r-1", 19420, "rv-1")  # shortfall 580 == 2.9% of 20000, fits shipping
    ds = decisions_by_id([rv], [r1])
    assert ds["r-1"].outcome == "matched"
    assert ds["r-1"].rule_id == "R4"
    assert ds["r-1"].adjustments == [("rv-1", 580, "tolerance")]


def test_r4_just_over_percent_escalates():
    rv = receivable("rv-1", total=20000, shipping=1000)
    r1 = receipt("r-1", 19419, "rv-1")  # shortfall 581 -> 2.905%
    ds = decisions_by_id([rv], [r1])
    assert ds["r-1"].outcome == "escalated"
    assert ds["r-1"].reason_code == ReasonCode.SHORT_PAY


def test_r4_shortfall_over_shipping_refused():
    rv = receivable("rv-1", total=20000, shipping=100)
    r1 = receipt("r-1", 19420, "rv-1")  # shortfall 580, within 2.9% but > shipping (100)
    ds = decisions_by_id([rv], [r1])
    assert ds["r-1"].outcome == "escalated"
    refused = {t["rule"] for t in ds["r-1"].evidence["rules_tried"]}
    assert "R4" in refused
    last = ds["r-1"].evidence["rules_tried"][-1]
    assert "shipping" in last["refused_because"]


# --- R6: overpayment / credit ---------------------------------------------------


def test_r6_overpayment_holds_credit():
    rv = receivable("rv-1", total=1000)
    r1 = receipt("r-1", 1500, "rv-1")
    ds = decisions_by_id([rv], [r1])
    assert ds["r-1"].outcome == "escalated"
    assert ds["r-1"].rule_id == "R6"
    assert ds["r-1"].reason_code == ReasonCode.OVERPAYMENT
    assert ds["r-1"].allocations == [("rv-1", 1000)]
    assert ds["r-1"].unapplied_credit == 500


# --- duplicate fingerprint -------------------------------------------------------


def test_duplicate_fingerprint_escalates_both_never_auto_excludes():
    rv = receivable("rv-1", total=2000)
    r1 = receipt("r-1", 1000, "rv-1", received_at=dt(5))
    r2 = receipt("r-2", 1000, "rv-1", received_at=dt(5))
    ds = decisions_by_id([rv], [r1, r2])
    assert ds["r-1"].outcome == "escalated"
    assert ds["r-2"].outcome == "escalated"
    assert ds["r-1"].reason_code == ReasonCode.SUSPECTED_DUPLICATE
    assert ds["r-2"].reason_code == ReasonCode.SUSPECTED_DUPLICATE
    assert ds["r-1"].allocations == []
    assert ds["r-2"].allocations == []


# --- R8 evidence / rules_tried ---------------------------------------------------


def test_r8_evidence_has_receipt_and_candidates_on_escalation():
    rv1 = receivable("rv-1", total=500, issued_at=dt(1))
    rv2 = receivable("rv-2", total=500, issued_at=dt(2))
    r1 = receipt("r-1", 500, "", received_at=dt(10))
    ds = decisions_by_id([rv1, rv2], [r1])
    evidence = ds["r-1"].evidence
    assert evidence["receipt"]["id"] == "r-1"
    assert {c["id"] for c in evidence["candidate_receivables"]} == {"rv-1", "rv-2"}
    assert evidence["rules_tried"][0]["rule"] == "R2"


@pytest.mark.parametrize("count", [1])
def test_alias_links_counterparty_for_r2(count):
    rv = receivable("rv-1", total=500, issued_at=dt(1), counterparty_id="canonical")
    r1 = receipt("r-1", 500, "", received_at=dt(10), counterparty_id="variant")
    ds = decisions_by_id([rv], [r1], aliases={"variant": "canonical"})
    assert ds["r-1"].outcome == "matched"
    assert ds["r-1"].matched_by == "amount_date"
