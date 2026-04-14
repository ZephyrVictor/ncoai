from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field

import tiktoken

from QPS_test.common import BehaviorType


@dataclass(frozen=True, slots=True)
class TrafficProfile:
    name: str
    normal_ratio: float
    stop_ratio: float
    thread_switch_ratio: float

    def choose_behavior(self, rng: random.Random) -> BehaviorType:
        sample = rng.random()
        if sample < self.normal_ratio:
            return "normal"
        if sample < self.normal_ratio + self.stop_ratio:
            return "stop"
        return "thread_switch"


TRAFFIC_PROFILES: dict[str, TrafficProfile] = {
    "availability": TrafficProfile(
        name="availability",
        normal_ratio=1.0,
        stop_ratio=0.0,
        thread_switch_ratio=0.0,
    ),
    "chat_availability": TrafficProfile(
        name="chat_availability",
        normal_ratio=0.95,
        stop_ratio=0.05,
        thread_switch_ratio=0.0,
    ),
    "standard": TrafficProfile(
        name="standard",
        normal_ratio=0.85,
        stop_ratio=0.10,
        thread_switch_ratio=0.05,
    ),
}


@dataclass(frozen=True, slots=True)
class TopicTemplate:
    name: str
    title: str
    audience: str
    asks: tuple[str, ...]
    snippets: tuple[str, ...]


TOPIC_LIBRARY: tuple[TopicTemplate, ...] = (
    TopicTemplate(
        name="incident-review",
        title="a production incident follow-up",
        audience="an internal platform team",
        asks=(
            "Summarize the next actions we should take this round.",
            "Call out the most likely hidden failure mode.",
            "Explain what we should verify before the next deploy.",
        ),
        snippets=(
            "The service stayed online, but user actions became visibly slow during peak traffic.",
            "The model backend kept responding, while the application layer showed queueing and retries.",
            "The team wants concrete steps that reduce latency without changing business rules.",
            "Database connections looked healthy overall, yet some requests waited longer than expected.",
            "We care about both immediate mitigation and structural cleanup for the next sprint.",
            "The answer should help an engineer continue the previous thread instead of restarting from zero.",
            "Operational risk matters because the machine is hard to recover once it becomes unstable.",
            "We want explanations that connect symptoms, likely causes, and measurable checks.",
            "The previous turn already covered the first obvious hypothesis, so this turn should go deeper.",
            "Short bullet style is fine, but the reasoning still needs to be explicit and actionable.",
        ),
    ),
    TopicTemplate(
        name="capacity-plan",
        title="a capacity planning discussion",
        audience="a service owner who expects concrete sizing guidance",
        asks=(
            "Give a practical recommendation for the next capacity change.",
            "Point out the metric that should trigger another scale-up.",
            "Mention one tradeoff that could hurt user experience if ignored.",
        ),
        snippets=(
            "The current goal is to support more concurrent chats without making the frontend feel stuck.",
            "The team wants better user experience first and raw benchmark numbers second.",
            "The database and application layer appear to be the main sources of delay right now.",
            "We need language that sounds like an engineer talking to another engineer in an ongoing thread.",
            "The response should continue the earlier discussion about bottlenecks and safe tuning.",
            "The system already has some caching and pooling, but the gains are not yet sufficient.",
            "The result should include a small set of next checks instead of a large generic checklist.",
            "We care about protecting stability while still improving throughput for real users.",
            "The answer should reflect that we are iterating on a live service, not doing a greenfield design.",
            "Avoid repeating the same explanation from the earlier turns unless it directly supports a new point.",
        ),
    ),
    TopicTemplate(
        name="chat-product",
        title="an iterative chat product discussion",
        audience="a teammate tuning user experience for a live chatbot",
        asks=(
            "Refine the recommendation based on what the user already observed.",
            "Tell us how to keep the chat feeling responsive under load.",
            "Suggest one lightweight validation we can run after the change.",
        ),
        snippets=(
            "The user notices the send button reacts slowly even when the model itself is not saturated.",
            "Switching between conversations should feel instant because that is part of the product experience.",
            "The team is trying to avoid any change that alters permissions, quotas, or visible business logic.",
            "We still want the answer to acknowledge operational safety and rollback simplicity.",
            "The current thread already established that frontend slowness can come from backend data access.",
            "The next answer should help prioritize improvements instead of listing every possible optimization.",
            "The service runs in a constrained but well-provisioned environment with a local database and cache.",
            "The user wants to keep iterating quickly and avoid changes that are risky to recover from.",
            "The assistant should continue the conversation naturally, as if it remembers the earlier context.",
            "Practical tradeoffs matter more than abstract architecture talk in this particular exchange.",
        ),
    ),
    TopicTemplate(
        name="ops-handbook",
        title="an operations handbook update",
        audience="an on-call engineer preparing the next maintenance window",
        asks=(
            "Describe the next operational step we should take and why.",
            "Name the metric or log pattern that confirms improvement.",
            "State the rollback signal that should stop the change.",
        ),
        snippets=(
            "The on-call engineer needs a reply that is practical enough to use during a live maintenance window.",
            "The previous turns already discussed the obvious fix, so the new answer should add sharper detail.",
            "The response should preserve the same operational mindset and avoid product-level feature churn.",
            "We want the conversation to sound like a realistic engineering back-and-forth, not a canned benchmark.",
            "The team tracks latency, throughput, queueing, and error rates, but user perception is the main concern.",
            "Answers should tie database behavior, cache behavior, and application behavior together when helpful.",
            "The current thread is already focused on real-world deployment constraints and cautious tuning.",
            "The service should remain usable during the rollout, so the advice should favor incremental changes.",
            "The reply should highlight one thing to watch closely after the adjustment goes out.",
            "The end result should help the team decide what to change next without introducing unrelated work.",
        ),
    ),
)


def get_traffic_profile(name: str) -> TrafficProfile:
    try:
        return TRAFFIC_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown traffic profile: {name}") from exc


def count_messages_tokens(
    encoder: tiktoken.Encoding,
    system_prompt: str,
    user_prompt: str,
) -> int:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    total = 0
    for message in messages:
        total += 4
        total += len(encoder.encode(str(message["role"])))
        total += len(encoder.encode(message["content"]))
    return total + 2


@dataclass(slots=True)
class PromptFactory:
    system_prompt: str
    encoding_name: str
    input_token_target: int
    prompt_safety_tokens: int = 64
    max_completion_tokens: int = 500
    _encoder: tiktoken.Encoding = field(init=False, repr=False)
    _topic_index: dict[str, TopicTemplate] = field(init=False, repr=False)
    _target_low: int = field(init=False, repr=False)
    _target_high: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._encoder = tiktoken.get_encoding(self.encoding_name)
        self._topic_index = {item.name: item for item in TOPIC_LIBRARY}
        self._target_low = max(self.input_token_target - 20, 64)
        self._target_high = max(self.input_token_target + 20, self._target_low)

    @property
    def minimum_request_tokens(self) -> int:
        return (
            self.input_token_target
            + self.prompt_safety_tokens
            + self.max_completion_tokens
        )

    def estimate_text_tokens(self, text: str) -> int:
        return len(self._encoder.encode(text))

    def estimate_fragment_tokens(self, text: str) -> int:
        if not text:
            return 0
        return len(self._encoder.encode(text))

    def choose_topic_name(self, *, user_id: str, rollover_index: int) -> str:
        seed = f"{user_id}:{rollover_index}".encode("utf-8")
        digest = hashlib.sha1(seed).digest()
        index = digest[0] % len(TOPIC_LIBRARY)
        return TOPIC_LIBRARY[index].name

    def build_prompt(
        self,
        *,
        user_id: str,
        request_seq: int,
        conversation_turn_index: int,
        topic_name: str,
    ) -> str:
        topic = self._topic_index.get(topic_name)
        if topic is None:
            topic = TOPIC_LIBRARY[0]
        return self._resolve_prompt(
            topic=topic,
            user_id=user_id,
            request_seq=request_seq,
            conversation_turn_index=conversation_turn_index,
        )

    def _resolve_prompt(
        self,
        *,
        topic: TopicTemplate,
        user_id: str,
        request_seq: int,
        conversation_turn_index: int,
    ) -> str:
        snippets = self._ordered_snippets(
            topic=topic,
            user_id=user_id,
            request_seq=request_seq,
            conversation_turn_index=conversation_turn_index,
        )
        chosen: list[str] = []
        candidate = self._render_candidate(
            topic=topic,
            user_id=user_id,
            request_seq=request_seq,
            conversation_turn_index=conversation_turn_index,
            snippets=chosen,
        )
        for snippet in snippets:
            trial = self._render_candidate(
                topic=topic,
                user_id=user_id,
                request_seq=request_seq,
                conversation_turn_index=conversation_turn_index,
                snippets=[*chosen, snippet],
            )
            token_count = count_messages_tokens(
                self._encoder,
                self.system_prompt,
                trial,
            )
            if token_count > self._target_high and chosen:
                break
            chosen.append(snippet)
            candidate = trial
            if self._target_low <= token_count <= self._target_high:
                return candidate

        while count_messages_tokens(self._encoder, self.system_prompt, candidate) < self._target_low:
            chosen.append(
                "Please keep building on the previous turns instead of restarting the explanation from scratch."
            )
            candidate = self._render_candidate(
                topic=topic,
                user_id=user_id,
                request_seq=request_seq,
                conversation_turn_index=conversation_turn_index,
                snippets=chosen,
            )
        return candidate

    def _ordered_snippets(
        self,
        *,
        topic: TopicTemplate,
        user_id: str,
        request_seq: int,
        conversation_turn_index: int,
    ) -> list[str]:
        offset_seed = (
            f"{topic.name}:{user_id}:{request_seq}:{conversation_turn_index}".encode("utf-8")
        )
        offset = int(hashlib.sha1(offset_seed).hexdigest(), 16) % len(topic.snippets)
        return [
            topic.snippets[(offset + index) % len(topic.snippets)]
            for index in range(len(topic.snippets))
        ]

    def _render_candidate(
        self,
        *,
        topic: TopicTemplate,
        user_id: str,
        request_seq: int,
        conversation_turn_index: int,
        snippets: list[str],
    ) -> str:
        ask_a = topic.asks[(conversation_turn_index - 1) % len(topic.asks)]
        ask_b = topic.asks[conversation_turn_index % len(topic.asks)]
        lines = [
            f"I am continuing an ongoing conversation about {topic.title} for {topic.audience}.",
            (
                f"This is turn {conversation_turn_index} in the same chat session, and the earlier "
                "messages already contain background context that you should keep using."
            ),
            (
                f"The current speaker is load-test user {user_id}, request sequence {request_seq}, "
                "and the goal is to continue the discussion naturally instead of opening a new topic."
            ),
            "For this turn, please help with the following follow-up points:",
            f"1. {ask_a}",
            f"2. {ask_b}",
            "3. Keep the answer practical, specific, and suitable for a teammate who is about to act on it.",
            "Relevant context for this turn:",
        ]
        lines.extend(f"- {snippet}" for snippet in snippets)
        lines.append(
            "Please answer as if you remember the earlier turns, and focus on the next concrete step, the main risk, and the fastest validation."
        )
        return "\n".join(lines)
