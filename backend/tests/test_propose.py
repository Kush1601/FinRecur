"""Row-validation tests for fixes/propose.py (spec 3.4 step 5, 3.5). Fake client,
no network -- validates ids/amounts/before-values against a ProposeContext."""

import pytest

from finrecur.claude import cache as cache_module
from finrecur.fixes.propose import ProposeContext, _policy_lines, propose


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path)


class _FakeClient:
    def __init__(self, response: dict | None):
        self.response = response
        self.calls = 0

    def call_tool(self, system, user_content, tool_name, tool_schema):
        self.calls += 1
        return self.response


FEATURES = {"m1": {"reason_code": "short_pay", "receipt_id": "r1", "receipt_amount": 1000}}
CONTEXT = ProposeContext(
    receipt_ids=frozenset({"r1"}),
    receivable_ids=frozenset({"rv1"}),
    receipt_amounts={"r1": 1000},
    policy={"R4": {"max_percent": 2.9}},
)


def test_propose_returns_none_without_client_or_cache():
    assert propose("cause", ["m1"], FEATURES, CONTEXT, client=None) is None


def test_policy_provenance_is_not_sent_to_the_model_or_cache_key():
    lines = _policy_lines(
        {"R2": {"kind": "window_days", "window_days": 30, "source": "internal note"}}
    )
    assert lines == ["R2.kind: 'window_days'", "R2.window_days: 30"]


def test_propose_accepts_a_valid_f5_fee_deduction():
    client = _FakeClient(
        {
            "fix_type": "record_fee_deduction",
            "params": {"receipt_ids": ["r1"], "fee_percent": 2.5, "payment_type": "credit_card"},
            "summary": "Deduct a 2.5% processor fee on credit card receipts.",
            "alternative": None,
        }
    )
    result = propose("cause", ["m1"], FEATURES, CONTEXT, client=client)
    assert result is not None
    assert result.fix_type == "record_fee_deduction"


def test_propose_rejects_unknown_receipt_id():
    client = _FakeClient(
        {
            "fix_type": "record_fee_deduction",
            "params": {
                "receipt_ids": ["not-real"],
                "fee_percent": 2.5,
                "payment_type": "credit_card",
            },
            "summary": "x",
            "alternative": None,
        }
    )
    assert propose("cause", ["m1"], FEATURES, CONTEXT, client=client) is None


def test_propose_rejects_f6_when_before_does_not_match_policy():
    client = _FakeClient(
        {
            "fix_type": "amend_policy",
            "params": {
                "rule_id": "R4",
                "key": "max_percent",
                "before": 3.6,  # actual current value is 2.9
                "after": 3.2,
                "justification": "widen tolerance",
            },
            "summary": "x",
            "alternative": None,
        }
    )
    assert propose("cause", ["m1"], FEATURES, CONTEXT, client=client) is None


def test_propose_accepts_f6_when_before_matches_policy():
    client = _FakeClient(
        {
            "fix_type": "amend_policy",
            "params": {
                "rule_id": "R4",
                "key": "max_percent",
                "before": 2.9,
                "after": 3.2,
                "justification": "widen tolerance for freight-adjacent shortfalls",
            },
            "summary": "x",
            "alternative": None,
        }
    )
    result = propose("cause", ["m1"], FEATURES, CONTEXT, client=client)
    assert result is not None


def test_propose_rejects_split_receipt_when_allocations_do_not_sum():
    client = _FakeClient(
        {
            "fix_type": "split_receipt",
            "params": {
                "splits": [
                    {"receipt_id": "r1", "allocations": [{"receivable_id": "rv1", "amount": 500}]}
                ]
            },
            "summary": "x",
            "alternative": None,
        }
    )
    assert propose("cause", ["m1"], FEATURES, CONTEXT, client=client) is None


def test_propose_validates_the_alternative_fix_too():
    client = _FakeClient(
        {
            "fix_type": "manual_review",
            "params": {"reason": "unclear"},
            "summary": "x",
            "alternative": {
                "fix_type": "record_fee_deduction",
                "params": {
                    "receipt_ids": ["ghost"],
                    "fee_percent": 2.5,
                    "payment_type": "credit_card",
                },
                "summary": "y",
            },
        }
    )
    assert propose("cause", ["m1"], FEATURES, CONTEXT, client=client) is None


def test_propose_caches_across_calls():
    client = _FakeClient(
        {
            "fix_type": "manual_review",
            "params": {"reason": "unclear"},
            "summary": "x",
            "alternative": None,
        }
    )
    propose("cause", ["m1"], FEATURES, CONTEXT, client=client)
    propose("cause", ["m1"], FEATURES, CONTEXT, client=client)
    assert client.calls == 1


# --- placeholder-id rejection (review fix: Claude filled unknown ids with
# "<UNKNOWN>" and the old validation didn't check membership) -----------------

ALIAS_CONTEXT = ProposeContext(
    receipt_ids=frozenset({"r1"}),
    receivable_ids=frozenset(),
    receipt_amounts={"r1": 1000},
    policy={"R4": {"max_percent": 2.9}},
    group_counterparty_ids=frozenset({"cp-variant"}),
    candidate_canonical_ids=frozenset({"cp-canonical"}),
)


def test_propose_rejects_placeholder_alias_ids():
    client = _FakeClient(
        {
            "fix_type": "add_counterparty_alias",
            "params": {
                "alias_counterparty_id": "<UNKNOWN>",
                "canonical_counterparty_id": "<UNKNOWN>",
            },
            "summary": "x",
            "alternative": None,
        }
    )
    assert propose("cause", ["m1"], FEATURES, ALIAS_CONTEXT, client=client) is None


def test_propose_rejects_alias_canonical_not_in_candidates():
    client = _FakeClient(
        {
            "fix_type": "add_counterparty_alias",
            "params": {
                "alias_counterparty_id": "cp-variant",
                "canonical_counterparty_id": "some-made-up-id",
            },
            "summary": "x",
            "alternative": None,
        }
    )
    assert propose("cause", ["m1"], FEATURES, ALIAS_CONTEXT, client=client) is None


def test_propose_accepts_alias_to_a_real_candidate():
    client = _FakeClient(
        {
            "fix_type": "add_counterparty_alias",
            "params": {
                "alias_counterparty_id": "cp-variant",
                "canonical_counterparty_id": "cp-canonical",
            },
            "summary": "x",
            "alternative": None,
        }
    )
    result = propose("cause", ["m1"], FEATURES, ALIAS_CONTEXT, client=client)
    assert result is not None


REPAIR_CONTEXT = ProposeContext(
    receipt_ids=frozenset({"r1"}),
    receivable_ids=frozenset(),
    receipt_amounts={"r1": 1000},
    policy={},
    nearest_reference_target={"r1": "order-123"},
)


def test_propose_rejects_repair_reference_placeholder_to_ref():
    client = _FakeClient(
        {
            "fix_type": "repair_reference",
            "params": {"repairs": [{"receipt_id": "r1", "from_ref": "order-12x", "to_ref": "TBD"}]},
            "summary": "x",
            "alternative": None,
        }
    )
    assert propose("cause", ["m1"], FEATURES, REPAIR_CONTEXT, client=client) is None


def test_propose_rejects_repair_reference_to_ref_not_the_computed_nearest_id():
    client = _FakeClient(
        {
            "fix_type": "repair_reference",
            "params": {
                "repairs": [{"receipt_id": "r1", "from_ref": "order-12x", "to_ref": "order-999"}]
            },
            "summary": "x",
            "alternative": None,
        }
    )
    assert propose("cause", ["m1"], FEATURES, REPAIR_CONTEXT, client=client) is None


def test_propose_accepts_repair_reference_matching_the_computed_nearest_id():
    client = _FakeClient(
        {
            "fix_type": "repair_reference",
            "params": {
                "repairs": [{"receipt_id": "r1", "from_ref": "order-12x", "to_ref": "order-123"}]
            },
            "summary": "x",
            "alternative": None,
        }
    )
    result = propose("cause", ["m1"], FEATURES, REPAIR_CONTEXT, client=client)
    assert result is not None


def test_propose_rejects_mark_duplicate_placeholder_id():
    client = _FakeClient(
        {
            "fix_type": "mark_duplicate",
            "params": {"pairs": [{"receipt_id": "r1", "duplicate_of": "<UNKNOWN>"}]},
            "summary": "x",
            "alternative": None,
        }
    )
    assert propose("cause", ["m1"], FEATURES, CONTEXT, client=client) is None


def test_propose_rejects_mark_duplicate_self_reference():
    context = ProposeContext(
        receipt_ids=frozenset({"r1"}),
        receivable_ids=frozenset(),
        receipt_amounts={"r1": 1000},
        policy={},
    )
    client = _FakeClient(
        {
            "fix_type": "mark_duplicate",
            "params": {"pairs": [{"receipt_id": "r1", "duplicate_of": "r1"}]},
            "summary": "x",
            "alternative": None,
        }
    )
    assert propose("cause", ["m1"], FEATURES, context, client=client) is None


def test_propose_rejects_f6_unknown_rule_or_key():
    client = _FakeClient(
        {
            "fix_type": "amend_policy",
            "params": {
                "rule_id": "R99",
                "key": "made_up",
                "before": 1,
                "after": 2,
                "justification": "x",
            },
            "summary": "x",
            "alternative": None,
        }
    )
    assert propose("cause", ["m1"], FEATURES, CONTEXT, client=client) is None


def test_propose_rejects_f6_when_after_equals_before():
    client = _FakeClient(
        {
            "fix_type": "amend_policy",
            "params": {
                "rule_id": "R4",
                "key": "max_percent",
                "before": 2.9,
                "after": 2.9,
                "justification": "x",
            },
            "summary": "x",
            "alternative": None,
        }
    )
    assert propose("cause", ["m1"], FEATURES, CONTEXT, client=client) is None


def test_propose_rejects_manual_review_placeholder_reason():
    client = _FakeClient(
        {
            "fix_type": "manual_review",
            "params": {"reason": "TBD"},
            "summary": "x",
            "alternative": None,
        }
    )
    assert propose("cause", ["m1"], FEATURES, CONTEXT, client=client) is None
