"""Cache-by-input-hash tests (spec 3.9). No network -- these only exercise the
read/write/key functions against a temp cache dir."""

from finrecur.claude import cache as cache_module
from finrecur.claude.cache import cache_key, read_cached, write_cache


def test_cache_key_is_deterministic_for_the_same_input():
    a = cache_key("model", "system", {"type": "object"}, {"x": 1})
    b = cache_key("model", "system", {"type": "object"}, {"x": 1})
    assert a == b


def test_cache_key_changes_with_any_input_field():
    base = cache_key("model", "system", {"type": "object"}, {"x": 1})
    assert base != cache_key("other-model", "system", {"type": "object"}, {"x": 1})
    assert base != cache_key("model", "other-system", {"type": "object"}, {"x": 1})
    assert base != cache_key("model", "system", {"type": "array"}, {"x": 1})
    assert base != cache_key("model", "system", {"type": "object"}, {"x": 2})


def test_write_then_read_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path)
    key = cache_key("model", "system", {}, {"a": 1})
    assert read_cached(key) is None
    write_cache(key, {"hello": "world"})
    assert read_cached(key) == {"hello": "world"}


def test_read_cached_missing_key_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path)
    assert read_cached("does-not-exist") is None
