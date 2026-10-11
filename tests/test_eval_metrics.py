import math

import pytest

from bank_services.store import BankStore
from evals.metrics import pass_hat_k, percentile, wilson_interval
from evals.runner import bipartite_match, change_matches, match_changes, state_diff

# ---- Wilson interval and pass^k -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("successes", "n", "low", "high"),
    [
        (5, 10, 0.2366, 0.7634),
        (0, 10, 0.0, 0.2775),
        (10, 10, 0.7225, 1.0),
        (69, 69, 0.9473, 1.0),
        (81, 100, 0.7222, 0.8749),
    ],
)
def test_wilson_interval_known_values(successes: int, n: int, low: float, high: float) -> None:
    got_low, got_high = wilson_interval(successes, n)
    assert (round(got_low, 4), round(got_high, 4)) == (low, high)


def test_wilson_interval_empty() -> None:
    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_pass_hat_k_known_values() -> None:
    # one scenario, 2 successes out of 4 trials
    assert pass_hat_k([(4, 2)], 1) == 0.5
    assert pass_hat_k([(4, 2)], 2) == pytest.approx(1 / 6)  # C(2,2)/C(4,2)
    assert pass_hat_k([(4, 2)], 3) == 0.0
    # averaged over scenarios: (3/3, 1/3) at k=1 -> 2/3; at k=2 -> (1 + 0) / 2
    assert pass_hat_k([(3, 3), (3, 1)], 1) == pytest.approx(2 / 3)
    assert pass_hat_k([(3, 3), (3, 1)], 2) == pytest.approx(0.5)
    assert pass_hat_k([(5, 4)], 3) == pytest.approx(math.comb(4, 3) / math.comb(5, 3))


def test_pass_hat_k_is_non_increasing_in_k() -> None:
    data = [(5, 5), (5, 3), (5, 1), (5, 0)]
    values = [pass_hat_k(data, k) for k in range(1, 6)]
    assert values == sorted(values, reverse=True)


def test_percentile_linear_interpolation() -> None:
    assert percentile([], 50) is None
    assert percentile([3.0], 95) == 3.0
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile(list(range(1, 101)), 95) == pytest.approx(95.05)


# ---- state diff and change matching -------------------------------------------------------------------------------


def test_state_diff_reports_every_kind_of_change() -> None:
    store = BankStore()
    before = store.snapshot()
    state = store.state
    state["cards"]["card_1001_debit"]["status"] = "frozen"
    state["accounts"]["acct_1002"]["status"] = "closed"
    state["customers"]["u_1003"]["email"] = "x@example.com"
    state["disputes"]["dsp_7001"] = {"transaction_id": "txn_1001_s02", "reason": "duplicate"}
    state["flags"]["flg_8001"] = {"transaction_id": "txn_1001_a01", "reason": "fraud"}
    state["transfers"]["trf_9001"] = {"from_account": "acct_1001", "to_account": "acct_9999", "amount": 5.0}
    changes = state_diff(before, store.snapshot())
    assert sorted(changes, key=lambda c: c["kind"]) == [
        {"kind": "account", "id": "acct_1002", "value": "closed"},
        {"kind": "card", "id": "card_1001_debit", "value": "frozen"},
        {"kind": "contact", "id": "u_1003", "value": "x@example.com"},
        {"kind": "dispute", "id": "txn_1001_s02", "value": "duplicate"},
        {"kind": "flag", "id": "txn_1001_a01", "value": "fraud"},
        {"kind": "transfer", "id": "acct_1001", "value": {"to": "acct_9999", "amount": 5.0}},
    ]


def test_state_diff_of_identical_snapshots_is_empty() -> None:
    store = BankStore()
    assert state_diff(store.snapshot(), store.snapshot()) == []


def test_change_matching_with_any_of_ids_and_values() -> None:
    expected = {"kind": "dispute", "id": ["txn_a", "txn_b"], "value": "duplicate"}
    assert change_matches(expected, {"kind": "dispute", "id": "txn_b", "value": "duplicate"})
    assert not change_matches(expected, {"kind": "dispute", "id": "txn_c", "value": "duplicate"})
    assert not change_matches(expected, {"kind": "dispute", "id": "txn_a", "value": "unauthorized"})
    assert not change_matches(expected, {"kind": "flag", "id": "txn_a", "value": "duplicate"})
    assert change_matches({"kind": "flag", "id": "txn_a"}, {"kind": "flag", "id": "txn_a", "value": "anything"})
    assert change_matches(
        {"kind": "card", "id": "c", "value": ["frozen", "active"]}, {"kind": "card", "id": "c", "value": "active"}
    )


def test_any_of_ids_are_not_double_counted() -> None:
    expected = [{"kind": "dispute", "id": ["s01", "s02"]}]
    both = [
        {"kind": "dispute", "id": "s01", "value": "duplicate"},
        {"kind": "dispute", "id": "s02", "value": "duplicate"},
    ]
    missing, unexpected = match_changes(expected, both)
    assert missing == [] and len(unexpected) == 1  # disputing both duplicates is one change too many


def test_matching_finds_the_assignment_greedy_would_miss() -> None:
    # expected[0] accepts a or b, expected[1] only a: a greedy pass that gives "a" to expected[0] would fail.
    expected = [{"kind": "flag", "id": ["a", "b"]}, {"kind": "flag", "id": ["a"]}]
    actual = [{"kind": "flag", "id": "a", "value": "r"}, {"kind": "flag", "id": "b", "value": "r"}]
    assert match_changes(expected, actual) == ([], [])
    assert bipartite_match(expected, actual, change_matches) == {0: 1, 1: 0}


def test_missing_and_unexpected_changes() -> None:
    expected = [{"kind": "card", "id": "card_1", "value": "frozen"}]
    actual = [{"kind": "card", "id": "card_2", "value": "frozen"}]
    assert match_changes(expected, actual) == (expected, actual)
