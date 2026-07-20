"""Tests for the orchestration path registry, prompt_builder wiring, and agentic routing."""

from unittest.mock import Mock

from langchain_core.messages import AIMessage, HumanMessage

from app.agents.rag_graph import RagGraphNodes, _build_answer_prompt
from app.providers.contracts import RetrievalResult


class _StubAnswerGenerator:
    def __init__(self, message):
        self._message = message
        self.received_messages = None

    def invoke_messages(self, messages, tools=None):
        self.received_messages = list(messages)
        return self._message


def test_answer_uses_build_answer_prompt_by_default():
    generator = _StubAnswerGenerator(AIMessage(content="ok"))
    nodes = RagGraphNodes(retriever_provider=Mock(), answer_generator=generator)
    state = {"question": "q", "normalized_question": "q", "retrieval_result": None, "messages": []}

    nodes.answer(state)

    human_messages = [m for m in generator.received_messages if isinstance(m, HumanMessage)]
    assert human_messages[-1].content == _build_answer_prompt(state)


def test_answer_uses_custom_prompt_builder_when_provided():
    generator = _StubAnswerGenerator(AIMessage(content="ok"))
    nodes = RagGraphNodes(retriever_provider=Mock(), answer_generator=generator)
    state = {"question": "q", "normalized_question": "q", "retrieval_result": None, "messages": []}

    def custom_prompt(_state):
        return "CUSTOM PROMPT"

    nodes.answer(state, prompt_builder=custom_prompt)

    human_messages = [m for m in generator.received_messages if isinstance(m, HumanMessage)]
    assert human_messages[-1].content == "CUSTOM PROMPT"
