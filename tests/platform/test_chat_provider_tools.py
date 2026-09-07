"""Provider tool-calling contract tests (work item C, A4/A13 evidence).

Covers: OpenAI-compatible ``tools`` passthrough into the DashScope payload,
``tool_calls`` parsing on the non-streaming and streaming paths (including
the empty-prose tool-call turn), and the fail-closed unavailable port.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.chat.models import ChatProviderResponse
from app.chat.ports import ChatProviderRequest, UnavailableChatProviderPort
from app.platform.chat_provider import _StreamAccumulator
from app.platform.errors import PlatformError

_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

_TOOLS = (
    {
        "type": "function",
        "function": {
            "name": "search_retrieval",
            "description": "检索知识库",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
)

_TOOL_CALL_BODY = {
    "choices": [
        {
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "search_retrieval",
                            "arguments": '{"query": "RAGqs 检索架构"}',
                        },
                    }
                ],
            }
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 5},
}


def _request(tools: tuple = ()) -> ChatProviderRequest:
    return ChatProviderRequest(
        generation_id="gen_tools_1",
        owner_user_id="user_1",
        content="它呢？",
        effort_level="quick",
        candidate=None,
        context_items=(),
        tools=tools,
    )


def _capturing_provider(response_body: dict):
    from tests.platform.test_model_http_request_contract import (
        _provider,
        _RequestCapturingTransport,
    )

    transport = _RequestCapturingTransport(
        httpx.Response(200, json=response_body, request=httpx.Request("POST", "https://x"))
    )
    provider = _provider(transport)
    return provider, transport


def test_tools_pass_through_to_payload_verbatim() -> None:
    provider, transport = _capturing_provider(
        {"choices": [{"message": {"content": "answer"}}], "usage": {}}
    )
    provider.generate(_request(_TOOLS))
    payload = json.loads(transport.requests[0].content.decode("utf-8"))
    assert payload["tools"] == [dict(tool) for tool in _TOOLS]


def test_absent_tools_keeps_payload_free_of_tools_key() -> None:
    provider, transport = _capturing_provider(
        {"choices": [{"message": {"content": "answer"}}], "usage": {}}
    )
    provider.generate(_request())
    payload = json.loads(transport.requests[0].content.decode("utf-8"))
    assert "tools" not in payload


def test_tool_call_response_is_parsed_with_empty_prose() -> None:
    provider, _ = _capturing_provider(_TOOL_CALL_BODY)
    response = provider.generate(_request(_TOOLS))
    assert response.content == ""
    assert [dict(call) for call in response.tool_calls] == [
        {"id": "call_1", "name": "search_retrieval", "arguments": '{"query": "RAGqs 检索架构"}'}
    ]


def test_streaming_tool_call_fragments_accumulate() -> None:
    sink = _StreamAccumulator(on_delta=lambda _text: None)
    sink(
        json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "search_retrieval", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            }
        )
    )
    sink(
        json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '{"query": "它"}'}}
                            ]
                        }
                    }
                ]
            }
        )
    )
    assert sink.content() == ""
    assert sink.tool_calls() == (
        {"id": "call_1", "name": "search_retrieval", "arguments": '{"query": "它"}'},
    )


def test_unavailable_port_stays_fail_closed_with_tools() -> None:
    with pytest.raises(PlatformError) as raised:
        UnavailableChatProviderPort().generate(_request(_TOOLS))
    assert raised.value.code == "provider_unavailable"


def test_tool_call_response_defaults_unchanged() -> None:
    response = ChatProviderResponse(content="answer", input_tokens=1, output_tokens=2)
    assert response.tool_calls == ()
