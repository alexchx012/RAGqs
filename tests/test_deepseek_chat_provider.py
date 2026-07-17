from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.providers.deepseek import DeepSeekChatModelProvider


class FakeCompletion:
    def __init__(self, content="done"):
        message = type(
            "Message",
            (),
            {
                "content": content,
                "reasoning_content": None,
                "tool_calls": None,
            },
        )()
        choice = type(
            "Choice",
            (),
            {
                "message": message,
                "finish_reason": "stop",
            },
        )()
        self.choices = [choice]
        self.usage = None


class RecordingCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeCompletion(content="done")


class RecordingClient:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": RecordingCompletions()})()

    @property
    def responses(self):
        raise AssertionError("DeepSeek adapter must not use Responses API")


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
    assert request["messages"][3]["tool_call_id"] == "call-1"
