"""The bounded LRU that suppresses replayed guids."""
from __future__ import annotations

from relay.dedup import DedupCache


def test_first_seen_is_new_then_replay_is_false():
    cache = DedupCache(4)
    assert cache.check_and_add("a") is True
    assert cache.check_and_add("a") is False
    assert cache.check_and_add("b") is True


def test_eviction_is_bounded_and_oldest_first():
    cache = DedupCache(2)
    assert cache.check_and_add("a") is True
    assert cache.check_and_add("b") is True
    assert cache.check_and_add("c") is True
    assert len(cache) == 2
    assert "a" not in cache
    assert cache.check_and_add("a") is True  # evicted, so it counts as new again


def test_recently_used_key_survives_eviction():
    cache = DedupCache(2)
    cache.check_and_add("a")
    cache.check_and_add("b")
    cache.check_and_add("a")  # touch a, so b is now the oldest
    cache.check_and_add("c")
    assert "a" in cache
    assert "b" not in cache


def test_missing_guid_is_never_suppressed():
    cache = DedupCache(2)
    assert cache.check_and_add("") is True
    assert cache.check_and_add(None) is True
    assert len(cache) == 0


def test_capacity_must_be_positive():
    try:
        DedupCache(0)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("DedupCache(0) should raise")
