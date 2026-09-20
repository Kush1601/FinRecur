from finrecur.snapshot import snapshot_hash


def test_hash_changes_when_a_balance_changes():
    state = {"receivables": {"rv-1": {"remaining": 1000}}}
    policy = {"R4": {"max_percent": 2.9}}
    a = snapshot_hash(state, policy)
    state2 = {"receivables": {"rv-1": {"remaining": 900}}}
    b = snapshot_hash(state2, policy)
    assert a != b


def test_hash_stable_regardless_of_key_order():
    a = snapshot_hash(
        {"receivables": {"rv-1": {"remaining": 1000}}, "aliases": []}, {"R4": {"max_percent": 2.9}}
    )
    b = snapshot_hash(
        {"aliases": [], "receivables": {"rv-1": {"remaining": 1000}}}, {"R4": {"max_percent": 2.9}}
    )
    assert a == b


def test_hash_unaffected_by_unrelated_rows():
    state = {"receivables": {"rv-1": {"remaining": 1000}}}
    policy = {"R4": {"max_percent": 2.9}}
    a = snapshot_hash(state, policy)
    # A row that isn't part of the affected set never enters the hash input at all.
    b = snapshot_hash(state, policy)
    assert a == b


def test_hash_changes_when_policy_changes():
    state = {"receivables": {"rv-1": {"remaining": 1000}}}
    a = snapshot_hash(state, {"R4": {"max_percent": 2.9}})
    b = snapshot_hash(state, {"R4": {"max_percent": 3.5}})
    assert a != b
