import json
from pathlib import Path

from fastapi.testclient import TestClient

import app.deepseek_local_provider as provider


class FakeBridgeBase:
    force_new_chat = False
    sticky_marker = None
    sticky_reanchor_messages = None
    session_state_path = None
    reuse_persisted_chat = False


def test_chat_completions_returns_openai_compatible_tool_calls(monkeypatch):
    def fake_call(*, messages, tools, thinking_enabled):
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
        assert thinking_enabled is False
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

    class FakeBridge(FakeBridgeBase):
        def call(self, *, messages, tools, thinking_enabled=None, include_debug=False):
            assert include_debug is False
            return fake_call(messages=messages, tools=tools, thinking_enabled=thinking_enabled)

    monkeypatch.setattr(
        provider,
        "get_bridge",
        lambda model_name, request_user=None: (provider.get_model_spec("DeepSeekV4"), FakeBridge()),
    )

    client = TestClient(provider.app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "DeepSeekV4",
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
    assert body["model"] == "DeepSeekV4"
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
    class FakeBridge(FakeBridgeBase):
        def call(self, **_):
            return {
                "content": "done",
                "tool_calls": [],
            }

    monkeypatch.setattr(
        provider,
        "get_bridge",
        lambda model_name, request_user=None: (provider.get_model_spec("DeepSeekV4"), FakeBridge()),
    )

    client = TestClient(provider.app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "DeepSeekV4",
            "messages": [{"role": "user", "content": "say done"}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["choices"][0]["message"]["content"] == "done"


def test_chat_completions_promotes_bash_plaintext_to_tool_call(monkeypatch):
    class FakeBridge(FakeBridgeBase):
        def call(self, **_):
            return {
                "content": "bash\nCopy\nDownload\nls -la",
                "tool_calls": [],
            }

    monkeypatch.setattr(
        provider,
        "get_bridge",
        lambda model_name, request_user=None: (provider.get_model_spec("DeepSeekV4"), FakeBridge()),
    )

    client = TestClient(provider.app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "DeepSeekV4",
            "messages": [{"role": "user", "content": "看一下目录里有哪些文件"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "Bash",
                        "description": "Execute shell commands",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "command": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["command"],
                        },
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    tool_call = body["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["function"]["name"] == "Bash"
    assert json.loads(tool_call["function"]["arguments"])["command"] == "ls -la"


def test_chat_completions_promotes_bash_plaintext_with_intro_to_tool_call(monkeypatch):
    class FakeBridge(FakeBridgeBase):
        def call(self, **_):
            return {
                "content": "我来查看当前目录下的文件。\n\nbash\nCopy\nDownload\nls -la",
                "tool_calls": [],
            }

    monkeypatch.setattr(
        provider,
        "get_bridge",
        lambda model_name, request_user=None: (provider.get_model_spec("DeepSeekV4"), FakeBridge()),
    )

    client = TestClient(provider.app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "DeepSeekV4",
            "messages": [{"role": "user", "content": "看一下目录里有哪些文件"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "Bash",
                        "description": "Execute shell commands",
                        "parameters": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    tool_call = body["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["function"]["name"] == "Bash"
    assert json.loads(tool_call["function"]["arguments"])["command"] == "ls -la"


def test_chat_completions_keeps_plaintext_when_no_shell_tool_provided(monkeypatch):
    class FakeBridge(FakeBridgeBase):
        def call(self, **_):
            return {
                "content": "bash\nCopy\nDownload\nls -la",
                "tool_calls": [],
            }

    monkeypatch.setattr(
        provider,
        "get_bridge",
        lambda model_name, request_user=None: (provider.get_model_spec("DeepSeekV4"), FakeBridge()),
    )

    client = TestClient(provider.app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "DeepSeekV4",
            "messages": [{"role": "user", "content": "看一下目录里有哪些文件"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read files",
                        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["choices"][0]["message"]["content"].startswith("bash")


def test_chat_completions_promotes_fenced_bash_block_to_tool_call(monkeypatch):
    class FakeBridge(FakeBridgeBase):
        def call(self, **_):
            return {
                "content": "我来查看当前目录下的文件。\n\n```bash\nls -la\n```",
                "tool_calls": [],
            }

    monkeypatch.setattr(
        provider,
        "get_bridge",
        lambda model_name, request_user=None: (provider.get_model_spec("DeepSeekV4"), FakeBridge()),
    )

    client = TestClient(provider.app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "DeepSeekV4",
            "messages": [{"role": "user", "content": "看目录"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "Bash",
                        "description": "Execute shell commands",
                        "parameters": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    tool_call = body["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["function"]["name"] == "Bash"
    assert json.loads(tool_call["function"]["arguments"])["command"] == "ls -la"


def test_validate_tool_calls_against_schemas_drops_missing_required():
    payload = {
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "name": "Bash",
                "arguments": {"description": "run ls"},
            }
        ],
    }
    tools = [
        {
            "type": "function",
            "function": {
                "name": "Bash",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["command"],
                },
            },
        }
    ]

    updated = provider.validate_tool_calls_against_schemas(payload, tools)
    assert updated["tool_calls"] == []


def test_validate_tool_calls_against_schemas_keeps_valid_call():
    payload = {
        "content": "",
        "tool_calls": [
            {
                "id": "call_2",
                "name": "Bash",
                "arguments": {"command": "ls -la"},
            }
        ],
    }
    tools = [
        {
            "type": "function",
            "function": {
                "name": "Bash",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        }
    ]

    updated = provider.validate_tool_calls_against_schemas(payload, tools)
    assert len(updated["tool_calls"]) == 1


def test_validate_tool_calls_against_schemas_drops_calls_when_no_tools_declared():
    payload = {
        "content": "",
        "tool_calls": [
            {
                "id": "call_x",
                "name": "Bash",
                "arguments": {"command": "ls -la"},
            }
        ],
    }

    updated = provider.validate_tool_calls_against_schemas(payload, [])
    assert updated["tool_calls"] == []


def test_validate_tool_calls_against_schemas_normalizes_case_insensitive_tool_name():
    payload = {
        "content": "",
        "tool_calls": [
            {
                "id": "call_3",
                "name": "bash",
                "arguments": {"command": "ls -la"},
            }
        ],
    }
    tools = [
        {
            "type": "function",
            "function": {
                "name": "Bash",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        }
    ]

    updated = provider.validate_tool_calls_against_schemas(payload, tools)
    assert updated["tool_calls"][0]["name"] == "Bash"


def test_validate_tool_calls_against_schemas_keeps_ls_when_declared():
    payload = {
        "content": "",
        "tool_calls": [
            {
                "id": "ls_1",
                "name": "ls",
                "arguments": {
                    "description": "查看项目根目录结构",
                    "path": "/mnt/workspaces/downloads/my-poor-renderer-master",
                },
            }
        ],
    }
    tools = [
        {
            "type": "function",
            "function": {
                "name": "ls",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["path"],
                },
            },
        }
    ]

    updated = provider.validate_tool_calls_against_schemas(payload, tools)
    assert len(updated["tool_calls"]) == 1
    assert updated["tool_calls"][0]["name"] == "ls"
    assert updated["tool_calls"][0]["id"] == "ls_1"


def test_validate_tool_calls_against_schemas_maps_list_dir_alias_to_ls():
    payload = {
        "content": "",
        "tool_calls": [
            {
                "id": "ls_1",
                "name": "list_dir",
                "arguments": {
                    "description": "查看项目根目录结构",
                    "path": "/mnt/workspaces/downloads/my-poor-renderer-master",
                },
            }
        ],
    }
    tools = [
        {
            "type": "function",
            "function": {
                "name": "ls",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["path"],
                },
            },
        }
    ]

    updated = provider.validate_tool_calls_against_schemas(payload, tools)
    assert len(updated["tool_calls"]) == 1
    assert updated["tool_calls"][0]["name"] == "ls"
    assert updated["tool_calls"][0]["id"] == "ls_1"


def test_validate_tool_calls_against_schemas_drops_additional_properties():
    payload = {
        "content": "",
        "tool_calls": [
            {
                "id": "call_4",
                "name": "Bash",
                "arguments": {"command": "ls -la", "unexpected": 123},
            }
        ],
    }
    tools = [
        {
            "type": "function",
            "function": {
                "name": "Bash",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
        }
    ]

    updated = provider.validate_tool_calls_against_schemas(payload, tools)
    assert len(updated["tool_calls"]) == 1
    assert updated["tool_calls"][0]["arguments"] == {"command": "ls -la"}


def test_build_openai_assistant_message_rewrites_duplicate_tool_call_ids():
    payload = {
        "content": "",
        "tool_calls": [
            {"id": "ls_1", "name": "list_dir", "arguments": {"path": "/tmp/a"}},
            {"id": "ls_1", "name": "read_file", "arguments": {"path": "/tmp/a/README.md"}},
        ],
    }

    message, tool_calls, finish_reason = provider.build_openai_assistant_message(payload)

    assert finish_reason == "tool_calls"
    assert len(tool_calls) == 2
    assert tool_calls[0]["id"] != tool_calls[1]["id"]
    assert message["tool_calls"][0]["id"] == tool_calls[0]["id"]
    assert message["tool_calls"][1]["id"] == tool_calls[1]["id"]


def test_chat_completions_accepts_cherry_undefined_fields(monkeypatch):
    captured: dict[str, object] = {}

    class FakeBridge(FakeBridgeBase):
        def call(self, *, messages, tools, thinking_enabled=None, include_debug=False):
            captured["messages"] = messages
            captured["tools"] = tools
            captured["thinking_enabled"] = thinking_enabled
            return {"content": "hi", "tool_calls": []}

    monkeypatch.setattr(
        provider,
        "get_bridge",
        lambda model_name, request_user=None: (provider.get_model_spec("DeepSeekV4"), FakeBridge()),
    )

    client = TestClient(provider.app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "DeepSeekV4",
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
    assert captured["thinking_enabled"] is False


def test_anthropic_messages_reduces_parallel_tool_calls(monkeypatch):
    class FakeBridge(FakeBridgeBase):
        def call(self, **_):
            return {
                "content": "",
                "tool_calls": [
                    {"id": "tool_1", "name": "Bash", "arguments": {"command": "ls -la"}},
                    {"id": "tool_2", "name": "Bash", "arguments": {"command": "pwd"}},
                ],
            }

    monkeypatch.setattr(
        provider,
        "get_bridge",
        lambda model_name, request_user=None: (provider.get_model_spec("DeepSeekV4"), FakeBridge()),
    )

    client = TestClient(provider.app)
    response = client.post(
        "/v1/messages",
        json={
            "model": "DeepSeekV4",
            "messages": [{"role": "user", "content": "test"}],
            "tools": [
                {
                    "name": "Bash",
                    "description": "Execute shell commands",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                }
            ],
        },
        headers={"anthropic-version": "2023-06-01"},
    )

    assert response.status_code == 200
    body = response.json()
    tool_uses = [block for block in body["content"] if block.get("type") == "tool_use"]
    assert len(tool_uses) == 1
    assert tool_uses[0]["name"] == "Bash"
    assert str(tool_uses[0]["id"]).startswith("toolu_")


def test_anthropic_messages_rewrites_non_toolu_ids(monkeypatch):
    class FakeBridge(FakeBridgeBase):
        def call(self, **_):
            return {
                "content": "",
                "tool_calls": [
                    {"id": "bash_1", "name": "Bash", "arguments": {"command": "ls -la"}},
                ],
            }

    monkeypatch.setattr(
        provider,
        "get_bridge",
        lambda model_name, request_user=None: (provider.get_model_spec("DeepSeekV4"), FakeBridge()),
    )

    client = TestClient(provider.app)
    response = client.post(
        "/v1/messages",
        json={
            "model": "DeepSeekV4",
            "messages": [{"role": "user", "content": "test"}],
            "tools": [
                {
                    "name": "Bash",
                    "description": "Execute shell commands",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                }
            ],
        },
        headers={"anthropic-version": "2023-06-01"},
    )

    assert response.status_code == 200
    body = response.json()
    tool_uses = [block for block in body["content"] if block.get("type") == "tool_use"]
    assert len(tool_uses) == 1
    assert str(tool_uses[0]["id"]).startswith("toolu_")


def test_stream_response_can_include_usage(monkeypatch):
    class FakeBridge(FakeBridgeBase):
        def call(self, **_):
            return {"content": "hello", "tool_calls": []}

    monkeypatch.setattr(
        provider,
        "get_bridge",
        lambda model_name, request_user=None: (provider.get_model_spec("DeepSeekV4"), FakeBridge()),
    )

    client = TestClient(provider.app)
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "DeepSeekV4",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    assert '"usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}' in body
    assert "data: [DONE]" in body


def test_resolve_request_spec_sessionizes_sticky_model():
    spec = provider.resolve_request_spec("deepseek-web-deerflow-sticky", "thread/123")

    assert spec.force_new_chat is False
    assert spec.reuse_persisted_chat is True
    assert spec.sticky_marker is not None
    assert spec.sticky_marker.startswith("flowflow__system_prompt_v2::thread-123-")
    assert spec.session_state_path is not None
    assert Path(spec.session_state_path).name.startswith("deepseek-web-deerflow-session--thread-123-")


def test_resolve_request_spec_disables_sticky_reuse_without_session_key():
    spec = provider.resolve_request_spec("deepseek-web-deerflow-sticky", None)

    assert spec.force_new_chat is True
    assert spec.reuse_persisted_chat is False
    assert spec.sticky_marker is None
    assert spec.session_state_path is None


def test_chat_completions_passes_thinking_enabled_from_extra_body(monkeypatch):
    captured: dict[str, object] = {}

    class FakeBridge(FakeBridgeBase):
        def call(self, *, messages, tools, thinking_enabled=None, include_debug=False):
            captured["messages"] = messages
            captured["tools"] = tools
            captured["thinking_enabled"] = thinking_enabled
            captured["include_debug"] = include_debug
            return {"content": "hi", "tool_calls": []}

    monkeypatch.setattr(
        provider,
        "get_bridge",
        lambda model_name, request_user=None: (provider.get_model_spec("deepseek-web-deerflow"), FakeBridge()),
    )

    client = TestClient(provider.app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "deepseek-web-deerflow",
            "messages": [{"role": "user", "content": "hi"}],
            "extra_body": {"thinking_enabled": True},
        },
    )

    assert response.status_code == 200
    assert captured["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["tools"] == []
    assert captured["thinking_enabled"] is True
    assert captured["include_debug"] is False


def test_chat_completions_top_level_thinking_enabled_overrides_extra_body(monkeypatch):
    captured: dict[str, object] = {}

    class FakeBridge(FakeBridgeBase):
        def call(self, *, messages, tools, thinking_enabled=None, include_debug=False):
            captured["thinking_enabled"] = thinking_enabled
            return {"content": "hi", "tool_calls": []}

    monkeypatch.setattr(
        provider,
        "get_bridge",
        lambda model_name, request_user=None: (provider.get_model_spec("deepseek-web-deerflow"), FakeBridge()),
    )

    client = TestClient(provider.app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "deepseek-web-deerflow",
            "messages": [{"role": "user", "content": "hi"}],
            "thinking_enabled": False,
            "extra_body": {"thinking_enabled": True},
        },
    )

    assert response.status_code == 200
    assert captured["thinking_enabled"] is False


def test_debug_thinking_mode_calls_bridge(monkeypatch):
    captured: dict[str, object] = {}

    class FakeBridge(FakeBridgeBase):
        def debug_sync_thinking_mode(self, thinking_enabled, *, visible=False):
            captured["thinking_enabled"] = thinking_enabled
            captured["visible"] = visible
            return {
                "changed": True,
                "before": {"thinking_enabled": False},
                "after": {"thinking_enabled": True},
                "candidates": [{"label": "DeepThink"}],
            }

    monkeypatch.setattr(
        provider,
        "get_bridge",
        lambda model_name, request_user=None: (provider.get_model_spec("deepseek-web-deerflow"), FakeBridge()),
    )

    client = TestClient(provider.app)
    response = client.post(
        "/debug/thinking-mode",
        json={
            "model": "deepseek-web-deerflow",
            "thinking_enabled": True,
            "visible": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["after"]["thinking_enabled"] is True
    assert captured == {"thinking_enabled": True, "visible": True}
