import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.providers.deepseek import DeepSeekChatModel, DeepSeekChatModelProvider, DeepSeekProviderError


class FakeUsage:
    def __init__(self, prompt_tokens=0, completion_tokens=0, total_tokens=0):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


def fake_tool_call(call_id: str, name: str, arguments: str):
    function = type("Function", (), {"name": name, "arguments": arguments})()
    return type(
        "ToolCall",
        (),
        {
            "id": call_id,
            "type": "function",
            "function": function,
        },
    )()


class FakeCompletion:
    def __init__(
        self,
        content="done",
        reasoning_content=None,
        tool_calls=None,
        finish_reason="stop",
        usage=None,
    ):
        message = type(
            "Message",
            (),
            {
                "content": content,
                "reasoning_content": reasoning_content,
                "tool_calls": tool_calls,
            },
        )()
        choice = type(
            "Choice",
            (),
            {
                "message": message,
                "finish_reason": finish_reason,
            },
        )()
        self.choices = [choice]
        self.usage = usage


class RecordingCompletions:
    def __init__(self, response=None, stream_chunks=None, error=None):
        self.calls = []
        self._response = response if response is not None else FakeCompletion(content="done")
        self._stream_chunks = stream_chunks
        self._error = error

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        if kwargs.get("stream"):
            return iter(self._stream_chunks or [])
        return self._response


class RecordingClient:
    def __init__(self, completions=None):
        self.chat = type(
            "Chat",
            (),
            {"completions": completions or RecordingCompletions()},
        )()

    @property
    def responses(self):
        raise AssertionError("DeepSeek adapter must not use Responses API")


def model_with_completion(completion: FakeCompletion):
    completions = RecordingCompletions(response=completion)
    client = RecordingClient(completions=completions)
    model = DeepSeekChatModel(
        model_name="deepseek-v4-pro",
        client=client,
        streaming=False,
    )
    return model, completions


def fake_delta(content=None, tool_calls=None, finish_reason=None):
    delta = type(
        "Delta",
        (),
        {
            "content": content,
            "tool_calls": tool_calls,
            "reasoning_content": None,
        },
    )()
    choice = type(
        "Choice",
        (),
        {
            "delta": delta,
            "finish_reason": finish_reason,
        },
    )()
    return type(
        "Chunk",
        (),
        {
            "choices": [choice],
            "usage": None,
        },
    )()


def fake_usage_only(prompt_tokens, completion_tokens, total_tokens):
    return type(
        "Chunk",
        (),
        {
            "choices": [],
            "usage": FakeUsage(prompt_tokens, completion_tokens, total_tokens),
        },
    )()


def model_with_stream(chunks):
    completions = RecordingCompletions(stream_chunks=chunks)
    client = RecordingClient(completions=completions)
    model = DeepSeekChatModel(
        model_name="deepseek-v4-pro",
        client=client,
        streaming=True,
    )
    return model, completions


def model_that_raises(error: Exception):
    completions = RecordingCompletions(error=error)
    client = RecordingClient(completions=completions)
    model = DeepSeekChatModel(
        model_name="deepseek-v4-pro",
        client=client,
        streaming=False,
    )
    return model, completions


def test_deepseek_uses_chat_completions_and_preserves_tool_history():
    client = RecordingClient()
    model = DeepSeekChatModelProvider(
        api_key="ds-key", model_name="deepseek-v4-pro", base_url="https://api.deepseek.com",
        client_factory=lambda **_: client,
    ).create_chat_model(streaming=False)
    model.invoke([
        SystemMessage(content="system"),
        HumanMessage(content="question"),
        AIMessage(content="", additional_kwargs={
            "reasoning_content": "private reasoning",
            "deepseek_tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "lookup", "arguments": "{\"id\":\"7\"}"},
            }],
        }),
        ToolMessage(content="result", tool_call_id="call-1", name="lookup"),
    ])

    request = client.chat.completions.calls[0]
    assert request["model"] == "deepseek-v4-pro"
    assert request["stream"] is False
    assert [item["role"] for item in request["messages"]] == ["system", "user", "assistant", "tool"]
    assert request["messages"][2]["reasoning_content"] == "private reasoning"
    assert "tool_calls" in request["messages"][2]
    assert request["messages"][2]["tool_calls"] == [{
        "id": "call-1",
        "type": "function",
        "function": {"name": "lookup", "arguments": '{"id":"7"}'},
    }]
    assert request["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"id":"7"}'
    assert request["messages"][3]["tool_call_id"] == "call-1"


def test_non_streaming_response_preserves_reasoning_raw_tool_arguments_and_usage():
    model, _ = model_with_completion(
        FakeCompletion(
            content="answer",
            reasoning_content="internal",
            tool_calls=[fake_tool_call("call-1", "lookup", '{"id":"7"}')],
            finish_reason="tool_calls",
            usage=FakeUsage(prompt_tokens=3, completion_tokens=5, total_tokens=8),
        )
    )

    response = model.invoke([HumanMessage(content="question")])

    assert response.content == "answer"
    assert response.additional_kwargs["reasoning_content"] == "internal"
    assert response.additional_kwargs["deepseek_tool_calls"][0]["function"]["arguments"] == '{"id":"7"}'
    assert response.response_metadata["finish_reason"] == "tool_calls"
    assert response.usage_metadata == {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8}


def test_stream_ignores_empty_and_usage_only_chunks():
    model, _ = model_with_stream([
        fake_delta(content="hel"), fake_delta(content=None), fake_usage_only(4, 2, 6), fake_delta(content="lo"),
    ])

    assert "".join(chunk.content for chunk in model.stream([HumanMessage(content="q")])) == "hello"


def test_transport_error_is_classified_without_exposing_api_key():
    model, _ = model_that_raises(RuntimeError("401 invalid key ds-secret"))

    with pytest.raises(DeepSeekProviderError, match="authentication") as error:
        model.invoke([HumanMessage(content="q")])
    assert "ds-secret" not in str(error.value)
