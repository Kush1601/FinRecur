"""Row-validation tests for grouping/explain.py (spec 3.4 step 4b). Uses a fake
client instead of the network -- these test the contract (schema + row validation +
cache write), not the model's behaviour, which is covered separately against the
committed cache fixtures."""

import pytest

from finrecur.claude import cache as cache_module
from finrecur.grouping.explain import explain


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


FEATURES = {
    "m1": {"reason_code": "short_pay", "shortfall_pct": 2.5, "payment_type": "credit_card"},
    "m2": {"reason_code": "short_pay", "shortfall_pct": 2.51, "payment_type": "credit_card"},
    "m3": {"reason_code": "short_pay", "shortfall_pct": 2.49, "payment_type": "credit_card"},
}


def test_explain_returns_none_without_client_or_cache():
    result = explain(list(FEATURES), FEATURES, "3 receipts", client=None)
    assert result is None


def test_explain_accepts_a_valid_response_and_writes_the_cache():
    client = _FakeClient(
        {
            "cause": "Processor fee netted these credit card receipts by 2.5%.",
            "member_ids_confirmed": ["m1", "m2", "m3"],
            "member_ids_dropped": [],
            "evidence_cited": ["shortfall_pct", "credit_card"],
            "confidence": 0.9,
        }
    )
    result = explain(list(FEATURES), FEATURES, "3 receipts", client=client)
    assert result is not None
    assert result.member_ids_confirmed == ["m1", "m2", "m3"]
    assert client.calls == 1

    # second call hits the cache, never calls the client again
    result2 = explain(list(FEATURES), FEATURES, "3 receipts", client=client)
    assert result2 == result
    assert client.calls == 1


def test_explain_rejects_an_invented_confirmed_id():
    client = _FakeClient(
        {
            "cause": "cause",
            "member_ids_confirmed": ["m1", "not-a-real-id"],
            "member_ids_dropped": [],
            "evidence_cited": ["shortfall_pct"],
            "confidence": 0.5,
        }
    )
    assert explain(list(FEATURES), FEATURES, "summary", client=client) is None


def test_explain_rejects_an_invented_dropped_id():
    client = _FakeClient(
        {
            "cause": "cause",
            "member_ids_confirmed": ["m1"],
            "member_ids_dropped": ["ghost"],
            "evidence_cited": ["shortfall_pct"],
            "confidence": 0.5,
        }
    )
    assert explain(list(FEATURES), FEATURES, "summary", client=client) is None


def test_explain_rejects_evidence_not_present_in_the_input():
    client = _FakeClient(
        {
            "cause": "cause",
            "member_ids_confirmed": ["m1"],
            "member_ids_dropped": [],
            "evidence_cited": ["a totally unrelated memo about a phone call"],
            "confidence": 0.5,
        }
    )
    assert explain(list(FEATURES), FEATURES, "summary", client=client) is None


def test_explain_allows_dropping_a_member():
    client = _FakeClient(
        {
            "cause": "cause",
            "member_ids_confirmed": ["m1", "m2"],
            "member_ids_dropped": ["m3"],
            "evidence_cited": ["shortfall_pct"],
            "confidence": 0.7,
        }
    )
    result = explain(list(FEATURES), FEATURES, "summary", client=client)
    assert result is not None
    assert result.member_ids_dropped == ["m3"]


def test_explain_rejects_schema_invalid_response():
    client = _FakeClient(
        {
            "cause": "x" * 400,
            "member_ids_confirmed": [],
            "member_ids_dropped": [],
            "evidence_cited": [],
            "confidence": 0.5,
        }
    )
    assert explain(list(FEATURES), FEATURES, "summary", client=client) is None
