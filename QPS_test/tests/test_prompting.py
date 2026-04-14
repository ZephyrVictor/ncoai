from __future__ import annotations

from QPS_test.prompting import PromptFactory, get_traffic_profile


def test_prompt_factory_builds_distinct_behavior_prompts() -> None:
    factory = PromptFactory(
        system_prompt="You are a concise and helpful assistant.",
        encoding_name="cl100k_base",
        input_token_target=200,
        prompt_safety_tokens=64,
        max_completion_tokens=500,
    )

    topic_name = factory.choose_topic_name(user_id="user-a", rollover_index=1)
    first = factory.build_prompt(
        user_id="user-a",
        request_seq=1,
        conversation_turn_index=1,
        topic_name=topic_name,
    )
    second = factory.build_prompt(
        user_id="user-a",
        request_seq=2,
        conversation_turn_index=2,
        topic_name=topic_name,
    )

    assert "turn 1" in first
    assert "turn 2" in second
    assert factory.estimate_text_tokens(first) >= 180
    assert factory.estimate_text_tokens(first) <= 220
    assert topic_name
    assert factory.minimum_request_tokens == 764


def test_standard_traffic_profile_respects_thresholds(monkeypatch) -> None:
    profile = get_traffic_profile("standard")

    class StubRandom:
        def __init__(self, values):
            self._values = iter(values)

        def random(self):
            return next(self._values)

    rng = StubRandom([0.10, 0.90, 0.99])
    assert profile.choose_behavior(rng) == "normal"
    assert profile.choose_behavior(rng) == "stop"
    assert profile.choose_behavior(rng) == "thread_switch"


def test_availability_traffic_profile_only_uses_normal_behavior() -> None:
    profile = get_traffic_profile("availability")

    class StubRandom:
        def random(self):
            return 0.999

    assert profile.choose_behavior(StubRandom()) == "normal"


def test_chat_availability_profile_allows_small_stop_ratio() -> None:
    profile = get_traffic_profile("chat_availability")

    class StubRandom:
        def __init__(self, values):
            self._values = iter(values)

        def random(self):
            return next(self._values)

    rng = StubRandom([0.10, 0.96, 0.999])
    assert profile.choose_behavior(rng) == "normal"
    assert profile.choose_behavior(rng) == "stop"
    assert profile.choose_behavior(rng) == "stop"
