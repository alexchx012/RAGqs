"""Named system prompt profiles for reusable RAG agents."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptProfile:
    name: str
    description: str
    system_prompt: str


class PromptProfileRegistry:
    """Registry for named system prompt profiles."""

    def __init__(self):
        self._profiles: dict[str, PromptProfile] = {}

    def register(self, profile: PromptProfile) -> None:
        if profile.name in self._profiles:
            raise ValueError(f"prompt profile already registered: {profile.name}")
        self._profiles[profile.name] = profile

    def names(self) -> list[str]:
        return list(self._profiles.keys())

    def get(self, name: str) -> PromptProfile:
        if name not in self._profiles:
            raise KeyError(f"unknown prompt profile: {name}")
        return self._profiles[name]


def build_default_prompt_registry() -> PromptProfileRegistry:
    registry = PromptProfileRegistry()
    registry.register(
        PromptProfile(
            name="default",
            description="Balanced knowledge-base question answering",
            system_prompt=(
                "你是一个专业的知识库问答助手，能够检索知识库并提供准确的回答。\n\n"
                "工作原则:\n"
                "1. 当用户提问时，主动使用知识库检索工具获取相关信息\n"
                "2. 基于检索到的内容提供准确、专业的回答\n"
                "3. 如果知识库中没有相关信息，诚实告知用户\n"
                "4. 回答简洁明了，重点突出"
            ),
        )
    )
    registry.register(
        PromptProfile(
            name="strict",
            description="Grounded answers with explicit refusal when context is insufficient",
            system_prompt=(
                "你是一个严格基于知识库回答的 RAG 助手。\n\n"
                "要求:\n"
                "1. 回答前必须优先检索知识库\n"
                "2. 只使用检索结果中能支撑的信息回答\n"
                "3. 知识库中没有足够依据时，直接说明无法从当前知识库确认\n"
                "4. 不编造来源、数据、结论或业务规则"
            ),
        )
    )
    registry.register(
        PromptProfile(
            name="concise",
            description="Short operational answers for repeated internal use",
            system_prompt=(
                "你是一个面向内部业务操作的知识库问答助手。"
                "优先检索知识库，回答保持简短、可执行，并在信息不足时明确说明。"
            ),
        )
    )
    return registry


def build_system_prompt(profile_name: str, *, registry: PromptProfileRegistry | None = None) -> str:
    active_registry = registry or build_default_prompt_registry()
    return active_registry.get(profile_name).system_prompt
