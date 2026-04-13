import json

from fastapi.testclient import TestClient

import app.deepseek_local_provider as provider


def test_chat_completions_returns_openai_compatible_tool_calls(monkeypatch):
    def fake_call(*, messages, tools):
        assert messages == [{"role": "user", "content": "write a file"}]
        assert tools == [
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Write text content to a file",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                    },
                },
            }
        ]
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_123",
                    "name": "write_file",
                    "arguments": {
                        "path": "/mnt/user-data/outputs/README.md",
                        "content": "# Demo",
                    },
                }
            ],
        }

    class FakeBridge:
        def call(self, *, messages, tools, include_debug=False):
            assert include_debug is False
            return fake_call(messages=messages, tools=tools)

    monkeypatch.setattr(provider, "get_bridge", lambda model_name: ("deepseek-web-cherry", FakeBridge()))

    client = TestClient(provider.app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "deepseek-web-cherry",
            "messages": [{"role": "user", "content": "write a file"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "description": "Write text content to a file",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["path", "content"],
                        },
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "deepseek-web-cherry"
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    tool_call = body["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["id"] == "call_123"
    assert tool_call["type"] == "function"
    assert tool_call["function"]["name"] == "write_file"
    assert json.loads(tool_call["function"]["arguments"]) == {
        "path": "/mnt/user-data/outputs/README.md",
        "content": "# Demo",
    }


def test_chat_completions_returns_plain_text_when_no_tool_call(monkeypatch):
    class FakeBridge:
        def call(self, **_):
            return {
                "content": "done",
                "tool_calls": [],
            }

    monkeypatch.setattr(provider, "get_bridge", lambda model_name: ("deepseek-web-cherry", FakeBridge()))

    client = TestClient(provider.app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "deepseek-web-cherry",
            "messages": [{"role": "user", "content": "say done"}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["choices"][0]["message"]["content"] == "done"


def test_chat_completions_accepts_cherry_undefined_fields(monkeypatch):
    captured: dict[str, object] = {}

    class FakeBridge:
        def call(self, *, messages, tools, include_debug=False):
            captured["messages"] = messages
            captured["tools"] = tools
            return {"content": "hi", "tool_calls": []}

    monkeypatch.setattr(provider, "get_bridge", lambda model_name: ("deepseek-web-cherry", FakeBridge()))

    client = TestClient(provider.app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "deepseek-web-cherry",
            "user": "[undefined]",
            "tools": "[undefined]",
            "tool_choice": "[undefined]",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "stream_options": "[undefined]",
        },
    )

    assert response.status_code == 200
    assert captured["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["tools"] == []


def test_stream_response_can_include_usage(monkeypatch):
    class FakeBridge:
        def call(self, **_):
            return {"content": "hello", "tool_calls": []}

    monkeypatch.setattr(provider, "get_bridge", lambda model_name: ("deepseek-web-cherry", FakeBridge()))

    client = TestClient(provider.app)
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "deepseek-web-cherry",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    assert '"usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}' in body
    assert "data: [DONE]" in body
