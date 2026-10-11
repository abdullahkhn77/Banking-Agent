from collections import Counter, defaultdict

from agent.tools import REGISTRY
from bank_services.data import EXPECTED_ANOMALIES, build_seed
from evals.build_scenarios import CATEGORIES, SCENARIOS_PATH, build_scenarios, render
from evals.runner import OWNERS, load_scenarios

SCENARIOS = load_scenarios()
SEED = build_seed()
EXPECTED_COUNTS = {
    "card_freeze": 9,
    "card_unfreeze": 6,
    "dispute": 9,
    "anomaly_flag": 8,
    "anomaly_none": 4,
    "multi_step": 6,
    "info_lookup": 6,
    "user_declines": 4,
    "policy_rejection": 3,
    "adversarial": 10,
    "clarification": 4,
}


def _ids(value: str | list[str]) -> list[str]:
    return value if isinstance(value, list) else [value]


def test_committed_file_matches_the_generator() -> None:
    assert SCENARIOS_PATH.read_text(encoding="utf-8") == render(build_scenarios())


def test_generator_is_deterministic() -> None:
    assert render(build_scenarios()) == render(build_scenarios())


def test_counts_per_category() -> None:
    assert Counter(s["category"] for s in SCENARIOS) == EXPECTED_COUNTS
    assert set(EXPECTED_COUNTS) == set(CATEGORIES)


def test_split_is_stratified() -> None:
    splits: dict[str, Counter[str]] = defaultdict(Counter)
    for s in SCENARIOS:
        splits[s["category"]][s["split"]] += 1
    for category, counts in splits.items():
        assert counts["dev"] >= 1 and counts["test"] >= 2, category
    dev_share = sum(s["split"] == "dev" for s in SCENARIOS) / len(SCENARIOS)
    assert 0.3 <= dev_share <= 0.5


def test_ids_and_fields_are_valid() -> None:
    assert len({s["id"] for s in SCENARIOS}) == len(SCENARIOS)
    for s in SCENARIOS:
        assert s["difficulty"] in {"single_action", "multi_step", "adversarial"}
        assert s["split"] in {"dev", "test"}
        assert s["user_id"] in SEED["customers"]
        assert s["turns"] and all(t.strip() for t in s["turns"])
        assert set(s["approve_tools"]) <= set(REGISTRY)
        for group in s["required_reads"]:
            assert group and set(group) <= set(REGISTRY)


def test_expected_targets_exist_and_belong_to_the_customer() -> None:
    for s in SCENARIOS:
        for proposal in s["expected_proposals"] + s["allowed_proposals"]:
            assert proposal["tool"] in REGISTRY
            for ident in _ids(proposal["key"]):
                assert OWNERS[ident] == s["user_id"], (s["id"], ident)
        for change in s["expected_changes"]:
            for ident in _ids(change["id"]):
                assert OWNERS[ident] == s["user_id"], (s["id"], ident)


def test_anomaly_scenarios_use_the_ground_truth() -> None:
    for s in SCENARIOS:
        account = SEED["customers"][s["user_id"]]["account_ids"][0]
        if s["category"] == "anomaly_flag":
            assert sorted(c["id"] for c in s["expected_changes"]) == sorted(EXPECTED_ANOMALIES[account])
        if s["category"] == "anomaly_none":
            assert EXPECTED_ANOMALIES[account] == () and s["expected_changes"] == []


def test_card_coverage() -> None:
    frozen_targets = {c["id"] for s in SCENARIOS if s["category"] == "card_freeze" for c in s["expected_changes"]}
    unfrozen_targets = {c["id"] for s in SCENARIOS if s["category"] == "card_unfreeze" for c in s["expected_changes"]}
    assert frozen_targets == {c["id"] for c in SEED["cards"].values() if c["status"] == "active"}
    assert unfrozen_targets == {c["id"] for c in SEED["cards"].values() if c["status"] == "frozen"}


def test_phrasings_vary_within_each_intent() -> None:
    for category in EXPECTED_COUNTS:
        texts = {s["turns"][0] for s in SCENARIOS if s["category"] == category}
        assert len(texts) >= 2, category


def test_a_do_nothing_agent_cannot_pass_any_scenario_by_construction() -> None:
    """Scenarios with no expected change must check the reply (or require proposals)."""
    for s in SCENARIOS:
        if not s["expected_changes"]:
            assert s["response_must_include"] or s["require_proposals"], s["id"]


def test_clarification_and_decline_scenarios() -> None:
    for s in SCENARIOS:
        if s["category"] == "clarification":
            assert s["clarify_first"] and len(s["turns"]) == 2
        if s["category"] == "user_declines":
            assert s["require_proposals"] and s["approve_tools"] == [] and s["response_must_not_include"]


def test_references_only_call_registered_tools() -> None:
    for s in SCENARIOS:
        for step in s["reference"]:
            assert ("content" in step) != ("tool_calls" in step)
            for call in step.get("tool_calls", []):
                assert call["name"] in REGISTRY, (s["id"], call["name"])
