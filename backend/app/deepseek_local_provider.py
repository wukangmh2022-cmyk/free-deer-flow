from __future__ import annotations

import atexit
import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.responses import StreamingResponse
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as jsonschema_validate

from deerflow.models.deepseek_web_bridge import DeepSeekWebBridge

logger = logging.getLogger(__name__)

DEFAULT_URL = os.environ.get("DEEPSEEK_WEB_URL", "https://chat.deepseek.com/")
DEFAULT_HEADLESS = os.environ.get("DEEPSEEK_WEB_HEADLESS", "1") == "1"
DEFAULT_FORCE_NEW_CHAT = os.environ.get("DEEPSEEK_WEB_FORCE_NEW_CHAT", "0") == "1"
DEFAULT_MODEL_ID = os.environ.get("DEEPSEEK_LOCAL_MODEL", "DeepSeekV4")
INTERFACE_MODE = os.environ.get("DEEPSEEK_LOCAL_INTERFACE_MODE", "both").strip().lower()


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    profile_dir: str
    force_new_chat: bool = DEFAULT_FORCE_NEW_CHAT
    sticky_marker: str | None = None
    sticky_reanchor_messages: int | None = 24
    session_state_path: str | None = None
    reuse_persisted_chat: bool = False
    forced_thinking_enabled: bool | None = None


DEERFLOW_PROFILE_DIR = os.environ.get("DEEPSEEK_WEB_PROFILE_DEERFLOW", "~/.deerflow/profile-deerflow")
DEERFLOW_SESSION_STATE_PATH = os.environ.get(
    "DEEPSEEK_WEB_SESSION_STATE_DEERFLOW",
    "~/.deerflow/deepseek-web-deerflow-session.json",
)

MODEL_SPECS: dict[str, ModelSpec] = {
    "deepseek-web-deerflow": ModelSpec(
        model_id="deepseek-web-deerflow",
        profile_dir=DEERFLOW_PROFILE_DIR,
        force_new_chat=os.environ.get("DEEPSEEK_WEB_FORCE_NEW_CHAT_DEERFLOW", "1") == "1",
        sticky_marker=os.environ.get("DEEPSEEK_WEB_STICKY_MARKER_DEERFLOW", "flowflow__system_prompt_v2"),
        sticky_reanchor_messages=int(os.environ.get("DEEPSEEK_WEB_STICKY_REANCHOR_MESSAGES_DEERFLOW", "24")),
        session_state_path=DEERFLOW_SESSION_STATE_PATH,
    ),
    "deepseek-web-deerflow-sticky": ModelSpec(
        model_id="deepseek-web-deerflow-sticky",
        profile_dir=DEERFLOW_PROFILE_DIR,
        force_new_chat=False,
        sticky_marker=os.environ.get("DEEPSEEK_WEB_STICKY_MARKER_DEERFLOW", "flowflow__system_prompt_v2"),
        sticky_reanchor_messages=int(os.environ.get("DEEPSEEK_WEB_STICKY_REANCHOR_MESSAGES_DEERFLOW", "24")),
        session_state_path=DEERFLOW_SESSION_STATE_PATH,
        reuse_persisted_chat=True,
    ),
    "DeepSeekV4": ModelSpec(
        model_id="DeepSeekV4",
        profile_dir=DEERFLOW_PROFILE_DIR,
        force_new_chat=True,
        sticky_marker=None,
        sticky_reanchor_messages=None,
        session_state_path=None,
        reuse_persisted_chat=False,
        forced_thinking_enabled=False,
    ),
    "DeepSeekV4-thinking": ModelSpec(
        model_id="DeepSeekV4-thinking",
        profile_dir=DEERFLOW_PROFILE_DIR,
        force_new_chat=True,
        sticky_marker=None,
        sticky_reanchor_messages=None,
        session_state_path=None,
        reuse_persisted_chat=False,
        forced_thinking_enabled=True,
    ),
}

# Optional legacy alias for older configs.
MODEL_ALIASES = {
    "deepseek-web": "DeepSeekV4",
    "DeepSeek V4": "DeepSeekV4",
    "DeepSeek V4-thinking": "DeepSeekV4-thinking",
    "DeepSeekV3": "DeepSeekV4",
    "DeepSeekV3-thinking": "DeepSeekV4-thinking",
}


def is_interface_enabled(name: str) -> bool:
    mode = INTERFACE_MODE or "both"
    if mode not in {"openai", "anthropic", "both"}:
        mode = "both"
    return mode == "both" or mode == name

_bridges: dict[str, DeepSeekWebBridge] = {}
_playwright_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="deepseek-web")
_SESSION_KEY_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")
TOOL_NAME_ALIASES: dict[str, str] = {
    "ls": "ls",
    "listdir": "ls",
    "list_dir": "ls",
    "list-dir": "ls",
    "cat": "read_file",
    "readfile": "read_file",
    "read_file": "read_file",
    "read-file": "read_file",
    "writefile": "write_file",
    "write_file": "write_file",
    "write-file": "write_file",
    "shell": "Bash",
    "bash": "Bash",
}
WINDOWS_COMPAT_ENV = "DEEPSEEK_LOCAL_WINDOWS_COMPAT"
FORCE_WINDOWS_PATH_ENV = "DEEPSEEK_LOCAL_FORCE_WINDOWS_PATHS"
WINDOWS_PATH_ARG_KEYS = {"path", "file_path", "cwd", "workdir"}


def summarize_tool_calls(tool_calls: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for index, tool_call in enumerate(tool_calls or []):
        arguments = tool_call.get("arguments", {})
        if isinstance(arguments, str):
            argument_chars = len(arguments)
        else:
            argument_chars = len(json.dumps(arguments, ensure_ascii=False))
        summary.append(
            {
                "index": index,
                "id": tool_call.get("id"),
                "name": tool_call.get("name"),
                "argument_chars": argument_chars,
            }
        )
    return summary


def get_model_spec(model_name: str) -> ModelSpec:
    resolved_name = MODEL_ALIASES.get(model_name, model_name)
    spec = MODEL_SPECS.get(resolved_name)
    if spec is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model '{model_name}'. Available models: {', '.join(MODEL_SPECS)}",
        )
    return spec


def _normalize_session_key(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None

    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    normalized = _SESSION_KEY_SANITIZE_RE.sub("-", raw).strip("-.")
    if normalized:
        normalized = normalized[:48].rstrip("-.")
        return f"{normalized}-{digest}"
    return f"session-{digest}"


def _sessionize_state_path(base_path: str | None, session_key: str | None) -> str | None:
    if not base_path or not session_key:
        return base_path
    path = Path(base_path).expanduser()
    suffix = path.suffix or ".json"
    stem = path.stem if path.suffix else path.name
    sessionized = path.with_name(f"{stem}--{session_key}{suffix}")
    return str(sessionized)


def resolve_request_spec(model_name: str, request_user: str | None = None) -> ModelSpec:
    spec = get_model_spec(model_name)
    if not spec.reuse_persisted_chat:
        return spec

    session_key = _normalize_session_key(request_user)
    if session_key is None:
        # Without a per-thread key, sticky mode can leak prior webpage context.
        return replace(
            spec,
            force_new_chat=True,
            reuse_persisted_chat=False,
            sticky_marker=None,
            session_state_path=None,
        )

    sticky_marker = f"{spec.sticky_marker}::{session_key}" if spec.sticky_marker else session_key
    session_state_path = _sessionize_state_path(spec.session_state_path, session_key)
    return replace(
        spec,
        sticky_marker=sticky_marker,
        session_state_path=session_state_path,
    )


def _bridge_cache_key(spec: ModelSpec) -> str:
    if spec.model_id in {"DeepSeekV4", "DeepSeekV4-thinking"}:
        return "DeepSeekV4-shared"
    if spec.profile_dir == DEERFLOW_PROFILE_DIR and spec.session_state_path == DEERFLOW_SESSION_STATE_PATH:
        return "deepseek-web-deerflow-shared"
    return spec.model_id


def get_bridge(model_name: str, request_user: str | None = None) -> tuple[ModelSpec, DeepSeekWebBridge]:
    base_spec = get_model_spec(model_name)
    spec = resolve_request_spec(model_name, request_user)
    bridge = _bridges.get(_bridge_cache_key(base_spec))
    if bridge is None:
        bridge = DeepSeekWebBridge(
            url=DEFAULT_URL,
            user_data_dir=base_spec.profile_dir,
            headless=DEFAULT_HEADLESS,
            force_new_chat=base_spec.force_new_chat,
            sticky_marker=base_spec.sticky_marker,
            sticky_reanchor_messages=base_spec.sticky_reanchor_messages,
            session_state_path=base_spec.session_state_path,
            reuse_persisted_chat=base_spec.reuse_persisted_chat,
        )
        _bridges[_bridge_cache_key(base_spec)] = bridge
    return spec, bridge


def bridge_call_with_spec(
    bridge: DeepSeekWebBridge,
    *,
    spec: ModelSpec,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    thinking_enabled: bool | None = None,
    include_debug: bool = False,
    output_protocol: str = "openai",
) -> dict[str, Any]:
    return run_bridge_with_spec(
        bridge,
        spec=spec,
        operation=lambda: _bridge_call_compat(
            bridge,
            messages=messages,
            tools=tools,
            thinking_enabled=thinking_enabled,
            include_debug=include_debug,
            output_protocol=output_protocol,
        ),
    )


def _bridge_call_compat(
    bridge: DeepSeekWebBridge,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    thinking_enabled: bool | None,
    include_debug: bool,
    output_protocol: str,
) -> dict[str, Any]:
    try:
        return bridge.call(
            messages=messages,
            tools=tools,
            thinking_enabled=thinking_enabled,
            include_debug=include_debug,
            output_protocol=output_protocol,
        )
    except TypeError as exc:
        if "output_protocol" not in str(exc):
            raise
        return bridge.call(
            messages=messages,
            tools=tools,
            thinking_enabled=thinking_enabled,
            include_debug=include_debug,
        )


def _run_in_playwright_worker(operation):
    # Some callers can leave an event loop bound to the worker thread.
    # Playwright Sync API rejects that environment, so detach it explicitly.
    try:
        asyncio.set_event_loop(None)
    except Exception:
        pass
    return operation()


def close_bridges() -> None:
    for bridge in _bridges.values():
        bridge.close()
    _bridges.clear()
    _playwright_executor.shutdown(wait=False, cancel_futures=True)


atexit.register(close_bridges)

app = FastAPI(title="DeepSeek Localhost Provider", version="0.2.0")


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    content: Any = ""
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="ignore")

    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = Field(default=DEFAULT_MODEL_ID)
    messages: list[ChatMessage]
    tools: list[dict[str, Any]] | None = None
    stream: bool = False
    user: str | None = None
    thinking_enabled: bool | None = None
    extra_body: dict[str, Any] | None = None
    stream_options: StreamOptions | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_undefined_values(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        normalized = dict(data)
        undefined_like = {"[undefined]", "undefined", ""}
        for key, value in list(normalized.items()):
            if isinstance(value, str) and value in undefined_like:
                normalized[key] = None

        tools = normalized.get("tools")
        if isinstance(tools, str) and tools in undefined_like:
            normalized["tools"] = None

        stream_options = normalized.get("stream_options")
        if isinstance(stream_options, str) and stream_options in undefined_like:
            normalized["stream_options"] = None

        extra_body = normalized.get("extra_body")
        if isinstance(extra_body, str) and extra_body in undefined_like:
            normalized["extra_body"] = None

        return normalized


class DebugTraceRequest(ChatCompletionRequest):
    include_payload: bool = False


class ThinkingModeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = Field(default=DEFAULT_MODEL_ID)
    user: str | None = None
    thinking_enabled: bool | None = None
    visible: bool = False


class AnthropicMessageRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = Field(default=DEFAULT_MODEL_ID)
    messages: Any = Field(default_factory=list)
    system: Any = None
    tools: Any = None
    stream: bool = False
    user: str | None = None
    thinking_enabled: bool | None = None
    extra_body: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_undefined_values(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        undefined_like = {"[undefined]", "undefined", ""}
        for key, value in list(normalized.items()):
            if isinstance(value, str) and value in undefined_like:
                normalized[key] = None
        messages = normalized.get("messages")
        if messages is None:
            normalized["messages"] = []
        elif not isinstance(messages, list):
            normalized["messages"] = [messages] if isinstance(messages, dict | str) else []

        tools = normalized.get("tools")
        if tools is None:
            normalized["tools"] = None
        elif isinstance(tools, list):
            pass
        elif isinstance(tools, dict):
            normalized["tools"] = [tools]
        else:
            normalized["tools"] = None
        return normalized


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    logger.warning(
        "request validation error path=%s errors=%s body=%s",
        request.url.path,
        exc.errors(),
        exc.body,
    )
    return JSONResponse(status_code=400, content={"detail": exc.errors()})


class AnthropicCountTokensRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = Field(default=DEFAULT_MODEL_ID)
    messages: list[dict[str, Any]]
    system: Any = None


def resolve_request_thinking_enabled(request: ChatCompletionRequest) -> bool | None:
    if isinstance(request.thinking_enabled, bool):
        return request.thinking_enabled
    extra_body = request.extra_body or {}
    candidate = extra_body.get("thinking_enabled")
    if isinstance(candidate, bool):
        return candidate
    return None


def resolve_effective_thinking_enabled(
    requested_thinking_enabled: bool | None,
    *,
    spec: ModelSpec,
) -> bool | None:
    if isinstance(spec.forced_thinking_enabled, bool):
        return spec.forced_thinking_enabled
    return requested_thinking_enabled


def normalize_anthropic_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
            if isinstance(block, dict):
                nested = block.get("content")
                if nested is not None:
                    parts.append(normalize_anthropic_text(nested))
            elif block is not None:
                parts.append(str(block))
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        if "text" in content:
            return normalize_anthropic_text(content.get("text"))
        if "content" in content:
            return normalize_anthropic_text(content.get("content"))
        return json.dumps(content, ensure_ascii=False)
    return "" if content is None else str(content)


def anthropic_messages_to_bridge_payload(request: AnthropicMessageRequest) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []

    if request.system:
        payload.append({"role": "system", "content": normalize_anthropic_text(request.system)})

    for message in request.messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content", "")

        if role == "assistant":
            assistant_content = ""
            tool_calls: list[dict[str, Any]] = []
            if isinstance(content, list):
                text_parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "text":
                        text = block.get("text")
                        if isinstance(text, str):
                            text_parts.append(text)
                    elif block_type == "tool_use":
                        name = block.get("name")
                        if isinstance(name, str) and name:
                            tool_calls.append(
                                {
                                    "id": block.get("id") or f"toolu_{uuid.uuid4().hex[:12]}",
                                    "name": name,
                                    "arguments": block.get("input", {}) if isinstance(block.get("input"), dict) else {},
                                }
                            )
                assistant_content = "\n".join(part for part in text_parts if part)
            else:
                assistant_content = normalize_anthropic_text(content)

            item: dict[str, Any] = {"role": "assistant", "content": assistant_content}
            if tool_calls:
                item["tool_calls"] = tool_calls
            payload.append(item)
            continue

        if role == "user":
            text_parts: list[str] = []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "tool_result":
                        payload.append(
                            {
                                "role": "tool",
                                "tool_call_id": block.get("tool_use_id"),
                                "content": normalize_anthropic_text(block.get("content", "")),
                            }
                        )
                    elif block_type == "text":
                        text = block.get("text")
                        if isinstance(text, str):
                            text_parts.append(text)
                user_text = "\n".join(part for part in text_parts if part)
                if user_text:
                    payload.append({"role": "user", "content": user_text})
            else:
                payload.append({"role": "user", "content": normalize_anthropic_text(content)})
            continue

        if role == "tool":
            payload.append(
                {
                    "role": "tool",
                    "tool_call_id": message.get("tool_call_id"),
                    "content": normalize_anthropic_text(content),
                }
            )
            continue

        payload.append({"role": "user", "content": normalize_anthropic_text(content)})

    return payload


def anthropic_tools_to_openai_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
        )
    return out


def build_openai_assistant_message(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    assistant_content = payload.get("content", "")
    message: dict[str, Any] = {
        "role": "assistant",
        "content": assistant_content,
        "refusal": None,
    }
    tool_calls = payload.get("tool_calls") or []
    normalized_tool_calls: list[dict[str, Any]] = []
    if tool_calls:
        seen_ids: set[str] = set()
        message_tool_calls: list[dict[str, Any]] = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            name = tool_call.get("name")
            arguments = tool_call.get("arguments")
            if not isinstance(name, str) or not name:
                continue
            if isinstance(arguments, str):
                arguments_json = arguments
                arguments_obj: dict[str, Any] = {}
                try:
                    parsed = json.loads(arguments)
                    if isinstance(parsed, dict):
                        arguments_obj = parsed
                except Exception:
                    pass
            else:
                arguments_obj = arguments if isinstance(arguments, dict) else {}
                arguments_json = json.dumps(arguments_obj, ensure_ascii=False)

            candidate_id = tool_call.get("id")
            normalized_id = candidate_id if isinstance(candidate_id, str) and candidate_id else f"call_{uuid.uuid4().hex}"
            if normalized_id in seen_ids:
                normalized_id = f"call_{uuid.uuid4().hex}"
            seen_ids.add(normalized_id)

            normalized_tool_calls.append(
                {
                    "id": normalized_id,
                    "name": name,
                    "arguments": arguments_obj,
                }
            )
            message_tool_calls.append(
                {
                    "id": normalized_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": arguments_json,
                    },
                }
            )

        message["tool_calls"] = message_tool_calls
        if not str(assistant_content or "").strip():
            message["content"] = None

    finish_reason = "tool_calls" if normalized_tool_calls else "stop"
    return message, normalized_tool_calls, finish_reason


def _extract_openai_tool_names(tools: list[dict[str, Any]] | None) -> list[str]:
    names: list[str] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _pick_shell_tool_name(tool_names: list[str]) -> str | None:
    for preferred in ("Bash", "bash", "shell", "terminal"):
        for name in tool_names:
            if name == preferred:
                return name
    for name in tool_names:
        if name.lower() in {"bash", "shell", "terminal"}:
            return name
    return None


def _extract_shell_command_from_text(content: str) -> str | None:
    if not isinstance(content, str):
        return None
    stripped = content.strip()
    if not stripped:
        return None

    fenced = re.search(r"```(?:bash|sh|shell)\s*\n(.*?)```", stripped, re.IGNORECASE | re.DOTALL)
    if fenced:
        command = fenced.group(1).strip()
        if command:
            return command

    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if not lines:
        return None

    normalized = [line for line in lines if line.lower() not in {"copy", "download"}]
    if not normalized:
        return None

    marker_indexes = [idx for idx, line in enumerate(normalized) if line.lower() in {"bash", "sh", "shell"}]
    if marker_indexes:
        start_idx = marker_indexes[-1] + 1
        if start_idx < len(normalized):
            command = "\n".join(normalized[start_idx:]).strip()
            return command or None

    return None


def apply_text_tool_call_fallback(payload: dict[str, Any], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    if payload.get("tool_calls"):
        return payload

    shell_tool_name = _pick_shell_tool_name(_extract_openai_tool_names(tools))
    if not shell_tool_name:
        return payload

    command = _extract_shell_command_from_text(payload.get("content", ""))
    if not command:
        return payload

    updated = dict(payload)
    updated["content"] = ""
    updated["tool_calls"] = [
        {
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "name": shell_tool_name,
            "arguments": {
                "command": command,
                "description": "Execute shell command requested by assistant",
            },
        }
    ]
    logger.warning(
        "provider text->tool fallback activated tool=%s command_preview=%r",
        shell_tool_name,
        command[:160],
    )
    return updated


def _normalize_anthropic_tool_use_id(value: Any) -> str:
    raw = value.strip() if isinstance(value, str) else ""
    if raw.startswith("toolu_") and len(raw) > 6:
        return raw
    return f"toolu_{uuid.uuid4().hex}"


def _type_matches_json_schema(value: Any, expected_type: str) -> bool:
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    return True


def _extract_openai_tool_schema_map(tools: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        params = function.get("parameters")
        if isinstance(params, dict):
            out[name] = params
    return out


def _resolve_declared_tool_name(name: str, schema_map: dict[str, dict[str, Any]]) -> str | None:
    if name in schema_map:
        return name
    alias = TOOL_NAME_ALIASES.get(name.lower())
    if alias and alias in schema_map:
        return alias
    lowered = name.lower()
    by_lower = [declared for declared in schema_map if declared.lower() == lowered]
    if len(by_lower) == 1:
        return by_lower[0]
    compact = re.sub(r"[-_\\s]+", "", lowered)
    alias_compact = TOOL_NAME_ALIASES.get(compact)
    if alias_compact and alias_compact in schema_map:
        return alias_compact
    by_compact = [declared for declared in schema_map if re.sub(r"[-_\\s]+", "", declared.lower()) == compact]
    if len(by_compact) == 1:
        return by_compact[0]
    return None


def _is_windows_compat_enabled() -> bool:
    return os.name == "nt" or os.environ.get(WINDOWS_COMPAT_ENV, "0").strip() == "1"


def _should_force_windows_path_style() -> bool:
    return os.name == "nt" or os.environ.get(FORCE_WINDOWS_PATH_ENV, "0").strip() == "1"


def _normalize_tool_name_key(name: str) -> str:
    return re.sub(r"[-_\s]+", "", name.lower())


def _pick_first_non_empty(mapping: dict[str, Any], candidate_keys: tuple[str, ...]) -> Any:
    for key in candidate_keys:
        value = mapping.get(key)
        if isinstance(value, str):
            if value.strip():
                return value
            continue
        if value is not None:
            return value
    return None


def _normalize_windows_path_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    raw = value.strip()
    if not raw:
        return value
    # Keep URL-like values intact.
    if "://" in raw:
        return value
    # Normalize slash style for obvious Windows absolute/UNC paths.
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith("\\\\"):
        return raw.replace("/", "\\")
    return value


def _coerce_tool_arguments_for_compatibility(
    declared_tool_name: str,
    arguments: dict[str, Any],
    schema: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    updated = dict(arguments)
    rewritten = False
    key = _normalize_tool_name_key(declared_tool_name)

    if key in {"bash"}:
        if "command" not in updated:
            candidate = _pick_first_non_empty(updated, ("cmd", "script", "bash_command", "shell_command"))
            if isinstance(candidate, str) and candidate.strip():
                updated["command"] = candidate
                rewritten = True

    if key in {"execcommand"}:
        if "cmd" not in updated:
            candidate = _pick_first_non_empty(updated, ("command", "script", "shell_command"))
            if isinstance(candidate, str) and candidate.strip():
                updated["cmd"] = candidate
                rewritten = True

    if key in {"ls", "listdir", "listdirs"}:
        if "path" not in updated:
            candidate = _pick_first_non_empty(updated, ("directory", "dir", "target", "cwd", "workdir"))
            if isinstance(candidate, str) and candidate.strip():
                updated["path"] = candidate
                rewritten = True

    if key in {"readfile", "writefile"}:
        if "path" not in updated:
            candidate = _pick_first_non_empty(updated, ("file_path", "filepath", "file", "filename", "target"))
            if isinstance(candidate, str) and candidate.strip():
                updated["path"] = candidate
                rewritten = True
        if key == "writefile" and "content" not in updated:
            candidate = _pick_first_non_empty(updated, ("text", "contents", "body", "data"))
            if isinstance(candidate, str):
                updated["content"] = candidate
                rewritten = True

    if _should_force_windows_path_style():
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for prop_key in WINDOWS_PATH_ARG_KEYS:
                if prop_key in updated and prop_key in properties:
                    normalized = _normalize_windows_path_value(updated[prop_key])
                    if normalized != updated[prop_key]:
                        updated[prop_key] = normalized
                        rewritten = True

    return updated, rewritten


def validate_tool_calls_against_schemas(payload: dict[str, Any], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload

    raw_tool_calls = payload.get("tool_calls")
    if not isinstance(raw_tool_calls, list) or not raw_tool_calls:
        return payload

    schema_map = _extract_openai_tool_schema_map(tools)
    if not schema_map:
        logger.warning("provider dropped tool_calls because request declared no tools")
        updated = dict(payload)
        updated["tool_calls"] = []
        return updated

    valid_calls: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    rewritten = False

    for tool_call in raw_tool_calls:
        if not isinstance(tool_call, dict):
            continue
        name = tool_call.get("name")
        arguments = tool_call.get("arguments")
        if not isinstance(name, str) or not name or not isinstance(arguments, dict):
            dropped.append({"name": name, "reason": "invalid_name_or_arguments_type"})
            continue

        resolved_name = _resolve_declared_tool_name(name, schema_map)
        schema = schema_map.get(resolved_name or "")
        if not isinstance(schema, dict):
            dropped.append({"name": name, "reason": "tool_name_not_declared"})
            continue

        required = schema.get("required")
        properties = schema.get("properties")

        if _is_windows_compat_enabled():
            normalized_arguments, compat_rewritten = _coerce_tool_arguments_for_compatibility(
                resolved_name or name,
                arguments,
                schema,
            )
            if compat_rewritten:
                tool_call = dict(tool_call)
                tool_call["arguments"] = normalized_arguments
                arguments = normalized_arguments
                rewritten = True

        if isinstance(properties, dict):
            sanitized_arguments = {key: value for key, value in arguments.items() if key in properties}
            if sanitized_arguments.keys() != arguments.keys():
                tool_call = dict(tool_call)
                tool_call["arguments"] = sanitized_arguments
                arguments = sanitized_arguments
                rewritten = True

        if isinstance(required, list):
            missing = [key for key in required if key not in arguments]
            if missing:
                dropped.append({"name": name, "reason": "missing_required", "missing": missing})
                continue

        if isinstance(properties, dict):
            type_mismatches: list[str] = []
            for key, prop_schema in properties.items():
                if key not in arguments or not isinstance(prop_schema, dict):
                    continue
                expected_type = prop_schema.get("type")
                if isinstance(expected_type, str) and not _type_matches_json_schema(arguments.get(key), expected_type):
                    type_mismatches.append(f"{key}:{expected_type}")
            if type_mismatches:
                dropped.append({"name": name, "reason": "type_mismatch", "fields": type_mismatches})
                continue

        try:
            jsonschema_validate(instance=arguments, schema=schema)
        except JsonSchemaValidationError as exc:
            dropped.append(
                {
                    "name": name,
                    "resolved_name": resolved_name,
                    "reason": "jsonschema_validation_error",
                    "message": str(exc).split("\n")[0][:300],
                }
            )
            continue

        if resolved_name and resolved_name != name:
            tool_call = dict(tool_call)
            tool_call["name"] = resolved_name
            rewritten = True
        valid_calls.append(tool_call)

    if not dropped and not rewritten:
        return payload

    if dropped:
        logger.warning("provider dropped invalid tool_calls=%s", dropped)
    updated = dict(payload)
    updated["tool_calls"] = valid_calls
    return updated


def run_bridge_with_spec(
    bridge: DeepSeekWebBridge,
    *,
    spec: ModelSpec,
    operation,
):
    original_force_new_chat = bridge.force_new_chat
    original_sticky_marker = bridge.sticky_marker
    original_sticky_reanchor_messages = bridge.sticky_reanchor_messages
    original_session_state_path = bridge.session_state_path
    original_reuse_persisted_chat = bridge.reuse_persisted_chat
    try:
        bridge.force_new_chat = spec.force_new_chat
        bridge.sticky_marker = spec.sticky_marker
        bridge.sticky_reanchor_messages = spec.sticky_reanchor_messages
        bridge.session_state_path = spec.session_state_path
        bridge.reuse_persisted_chat = spec.reuse_persisted_chat
        return operation()
    finally:
        bridge.force_new_chat = original_force_new_chat
        bridge.sticky_marker = original_sticky_marker
        bridge.sticky_reanchor_messages = original_sticky_reanchor_messages
        bridge.session_state_path = original_session_state_path
        bridge.reuse_persisted_chat = original_reuse_persisted_chat


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/debug/open-login")
async def open_login(model: str = DEFAULT_MODEL_ID) -> dict[str, Any]:
    spec, bridge = get_bridge(model)
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(_playwright_executor, bridge.open_login_page)
    return {"model": spec.model_id, **result}


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    if not is_interface_enabled("openai"):
        raise HTTPException(status_code=404, detail="OpenAI-compatible endpoints are disabled by interface mode.")
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": spec.model_id,
                "object": "model",
                "created": now,
                "owned_by": "local",
            }
            for spec in MODEL_SPECS.values()
        ],
    }


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: ChatCompletionRequest):
    if not is_interface_enabled("openai"):
        raise HTTPException(status_code=404, detail="OpenAI-compatible endpoints are disabled by interface mode.")

    spec, bridge = get_bridge(request.model, request.user)
    resolved_model = spec.model_id
    request_id = uuid.uuid4().hex[:8]
    request_messages = [message.model_dump(exclude_none=True) for message in request.messages]
    request_tools = request.tools or []
    request_thinking_enabled = resolve_effective_thinking_enabled(
        resolve_request_thinking_enabled(request),
        spec=spec,
    )
    logger.warning(
        "provider[%s] /v1 start model=%s stream=%s messages=%d tools=%d thinking_enabled=%s",
        request_id,
        resolved_model,
        request.stream,
        len(request_messages),
        len(request_tools),
        request_thinking_enabled,
    )

    try:
        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(
            _playwright_executor,
            lambda: _run_in_playwright_worker(
                lambda: bridge_call_with_spec(
                    bridge,
                    spec=spec,
                    messages=request_messages,
                    tools=request_tools,
                    thinking_enabled=request_thinking_enabled,
                    output_protocol="openai",
                )
            ),
        )
    except Exception as exc:
        logger.exception("provider[%s] /v1 bridge.call failed", request_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    raw_text = payload.get("raw_text", "")
    logger.warning(
        "provider[%s] /v1 bridge.call done content_chars=%d raw_text_chars=%d tool_calls=%s",
        request_id,
        len(payload.get("content", "") or ""),
        len(raw_text) if isinstance(raw_text, str) else 0,
        summarize_tool_calls(payload.get("tool_calls")),
    )

    payload = apply_text_tool_call_fallback(payload, request_tools)
    payload = validate_tool_calls_against_schemas(payload, request_tools)
    message, tool_calls, finish_reason = build_openai_assistant_message(payload)
    if tool_calls:
        logger.warning(
            "provider[%s] /v1 tool_calls encoded summaries=%s",
            request_id,
            [
                {
                    "index": index,
                    "id": tool_call["id"],
                    "name": tool_call["function"]["name"],
                    "argument_chars": len(tool_call["function"]["arguments"]),
                }
                for index, tool_call in enumerate(message["tool_calls"])
            ],
        )

    if request.stream:
        logger.warning("provider[%s] /v1 returning stream finish_reason=%s", request_id, finish_reason)
        return StreamingResponse(
            stream_chat_completion_chunks(
                model=resolved_model,
                content=message.get("content", ""),
                tool_calls=message.get("tool_calls"),
                finish_reason=finish_reason,
                include_usage=bool(request.stream_options and request.stream_options.include_usage),
            ),
            media_type="text/event-stream",
        )

    response_body = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": resolved_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
                "logprobs": None,
            }
        ],
        "system_fingerprint": None,
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }
    logger.warning(
        "provider[%s] /v1 return ready finish_reason=%s response_chars=%d",
        request_id,
        finish_reason,
        len(json.dumps(response_body, ensure_ascii=False)),
    )
    return response_body


@app.post("/v1/messages", response_model=None)
async def anthropic_messages(request: AnthropicMessageRequest):
    if not is_interface_enabled("anthropic"):
        raise HTTPException(status_code=404, detail="Anthropic-compatible endpoints are disabled by interface mode.")

    spec, bridge = get_bridge(request.model, request.user)
    resolved_model = spec.model_id
    request_id = uuid.uuid4().hex[:8]
    bridge_messages = anthropic_messages_to_bridge_payload(request)
    request_tools = anthropic_tools_to_openai_tools(request.tools)
    request_thinking_enabled = resolve_effective_thinking_enabled(
        request.thinking_enabled,
        spec=spec,
    )

    logger.warning(
        "provider[%s] /v1/messages start model=%s stream=%s messages=%d tools=%d thinking_enabled=%s",
        request_id,
        resolved_model,
        request.stream,
        len(bridge_messages),
        len(request_tools),
        request_thinking_enabled,
    )

    try:
        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(
            _playwright_executor,
            lambda: _run_in_playwright_worker(
                lambda: bridge_call_with_spec(
                    bridge,
                    spec=spec,
                    messages=bridge_messages,
                    tools=request_tools,
                    thinking_enabled=request_thinking_enabled,
                    output_protocol="anthropic",
                )
            ),
        )
    except Exception as exc:
        logger.exception("provider[%s] /v1/messages bridge.call failed", request_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    payload = apply_text_tool_call_fallback(payload, request_tools)
    payload = validate_tool_calls_against_schemas(payload, request_tools)
    message, tool_calls, _ = build_openai_assistant_message(payload)
    if len(tool_calls) > 1:
        logger.warning(
            "provider[%s] /v1/messages reducing parallel tool_calls from %d to 1 for compatibility",
            request_id,
            len(tool_calls),
        )
        tool_calls = [tool_calls[0]]
    if tool_calls:
        logger.warning(
            "provider[%s] /v1/messages tool_calls accepted summaries=%s",
            request_id,
            summarize_tool_calls(tool_calls),
        )

    anthropic_content: list[dict[str, Any]] = []
    message_text = message.get("content")
    if isinstance(message_text, str) and message_text.strip():
        anthropic_content.append({"type": "text", "text": message_text})

    seen_tool_use_ids: set[str] = set()
    for tool_call in tool_calls:
        arguments = tool_call.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except Exception:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        normalized_tool_use_id = _normalize_anthropic_tool_use_id(tool_call.get("id"))
        while normalized_tool_use_id in seen_tool_use_ids:
            normalized_tool_use_id = f"toolu_{uuid.uuid4().hex}"
        seen_tool_use_ids.add(normalized_tool_use_id)
        anthropic_content.append(
            {
                "type": "tool_use",
                "id": normalized_tool_use_id,
                "name": tool_call.get("name"),
                "input": arguments,
            }
        )

    if not anthropic_content:
        anthropic_content = [{"type": "text", "text": ""}]

    stop_reason = "tool_use" if tool_calls else "end_turn"
    response_body = {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": resolved_model,
        "content": anthropic_content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }

    if request.stream:
        return StreamingResponse(
            stream_anthropic_message_events(response_body),
            media_type="text/event-stream",
        )

    return response_body


@app.post("/v1/messages/count_tokens", response_model=None)
async def anthropic_count_tokens(request: AnthropicCountTokensRequest):
    if not is_interface_enabled("anthropic"):
        raise HTTPException(status_code=404, detail="Anthropic-compatible endpoints are disabled by interface mode.")
    _ = request
    return {"input_tokens": 0}


@app.post("/debug/chat-timings")
async def debug_chat_timings(request: DebugTraceRequest) -> dict[str, Any]:
    spec, bridge = get_bridge(request.model)
    resolved_model = spec.model_id
    request_id = uuid.uuid4().hex[:8]
    request_messages = [message.model_dump(exclude_none=True) for message in request.messages]
    request_tools = request.tools or []
    request_thinking_enabled = resolve_effective_thinking_enabled(
        resolve_request_thinking_enabled(request),
        spec=spec,
    )
    logger.warning(
        "provider[%s] /debug start model=%s messages=%d tools=%d include_payload=%s thinking_enabled=%s",
        request_id,
        resolved_model,
        len(request_messages),
        len(request_tools),
        request.include_payload,
        request_thinking_enabled,
    )

    try:
        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(
            _playwright_executor,
            lambda: _run_in_playwright_worker(
                lambda: bridge_call_with_spec(
                    bridge,
                    spec=spec,
                    messages=request_messages,
                    tools=request_tools,
                    thinking_enabled=request_thinking_enabled,
                    include_debug=True,
                    output_protocol="openai",
                )
            ),
        )
    except Exception as exc:
        logger.exception("provider[%s] /debug bridge.call failed", request_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    logger.warning(
        "provider[%s] /debug bridge.call done content_chars=%d raw_text_chars=%d tool_calls=%s",
        request_id,
        len(payload.get("content", "") or ""),
        len(payload.get("raw_text", "") or ""),
        summarize_tool_calls(payload.get("tool_calls")),
    )

    response: dict[str, Any] = {
        "model": resolved_model,
        "timing": (payload.get("debug") or {}).get("timing", {}),
    }
    if request.include_payload:
        response["payload"] = {
            "content": payload.get("content", ""),
            "tool_calls": payload.get("tool_calls", []),
            "raw_text": payload.get("raw_text", ""),
        }
    logger.warning(
        "provider[%s] /debug return ready response_chars=%d include_payload=%s",
        request_id,
        len(json.dumps(response, ensure_ascii=False)),
        request.include_payload,
    )
    return response


@app.post("/debug/thinking-mode")
async def debug_thinking_mode(request: ThinkingModeRequest) -> dict[str, Any]:
    spec, bridge = get_bridge(request.model, request.user)
    resolved_model = spec.model_id
    request_id = uuid.uuid4().hex[:8]
    requested = request.thinking_enabled
    effective_requested = resolve_effective_thinking_enabled(requested, spec=spec)
    logger.warning(
        "provider[%s] /debug/thinking-mode start model=%s requested=%s visible=%s",
        request_id,
        resolved_model,
        effective_requested,
        request.visible,
    )

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _playwright_executor,
            lambda: _run_in_playwright_worker(
                lambda: run_bridge_with_spec(
                    bridge,
                    spec=spec,
                    operation=lambda: bridge.debug_sync_thinking_mode(
                        effective_requested,
                        visible=request.visible,
                    ),
                ),
            ),
        )
    except Exception as exc:
        logger.exception("provider[%s] /debug/thinking-mode failed", request_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    response = {
        "model": resolved_model,
        **result,
    }
    logger.warning(
        "provider[%s] /debug/thinking-mode done applied=%s current=%s changed=%s",
        request_id,
        effective_requested,
        result.get("after", {}).get("thinking_enabled"),
        result.get("changed"),
    )
    return response


def stream_chat_completion_chunks(
    *,
    model: str,
    content: str,
    tool_calls: list[dict[str, Any]] | None,
    finish_reason: str,
    include_usage: bool = False,
) -> Iterator[str]:
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    initial_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant"},
                "finish_reason": None,
                "logprobs": None,
            }
        ],
        "system_fingerprint": None,
    }
    yield f"data: {json.dumps(initial_chunk, ensure_ascii=False)}\n\n"

    if tool_calls:
        for index, tool_call in enumerate(tool_calls):
            tool_chunk = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": tool_call.get("id"),
                                    "type": "function",
                                    "function": {
                                        "name": (tool_call.get("function") or {}).get("name"),
                                        "arguments": (tool_call.get("function") or {}).get("arguments", ""),
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                        "logprobs": None,
                    }
                ],
                "system_fingerprint": None,
            }
            yield f"data: {json.dumps(tool_chunk, ensure_ascii=False)}\n\n"
    elif content:
        for piece in iter_simulated_stream_pieces(content):
            content_chunk = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": piece},
                        "finish_reason": None,
                        "logprobs": None,
                    }
                ],
                "system_fingerprint": None,
            }
            yield f"data: {json.dumps(content_chunk, ensure_ascii=False)}\n\n"
            time.sleep(0.03)

    final_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": finish_reason,
                "logprobs": None,
            }
        ],
        "system_fingerprint": None,
    }
    yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n"

    if include_usage:
        usage_chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            "system_fingerprint": None,
        }
        yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"

    yield "data: [DONE]\n\n"


def stream_anthropic_message_events(response_body: dict[str, Any]) -> Iterator[str]:
    def emit(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    content_blocks = response_body.get("content") or []
    message_stub = {
        "id": response_body.get("id"),
        "type": "message",
        "role": "assistant",
        "model": response_body.get("model"),
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    yield emit("message_start", {"type": "message_start", "message": message_stub})

    for index, block in enumerate(content_blocks):
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text", "")
            yield emit(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            for piece in iter_simulated_stream_pieces(text):
                yield emit(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": piece},
                    },
                )
            yield emit("content_block_stop", {"type": "content_block_stop", "index": index})
            continue

        if block_type == "tool_use":
            yield emit(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": block.get("input", {}),
                    },
                },
            )
            yield emit("content_block_stop", {"type": "content_block_stop", "index": index})

    yield emit(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": response_body.get("stop_reason"),
                "stop_sequence": response_body.get("stop_sequence"),
            },
            "usage": response_body.get("usage", {"output_tokens": 0}),
        },
    )
    yield emit("message_stop", {"type": "message_stop"})


def iter_simulated_stream_pieces(content: str, target_chunk_size: int = 12) -> Iterator[str]:
    content = content or ""
    if not content:
        return

    current = ""
    for char in content:
        current += char
        boundary = char.isspace() or char in ",.!?;:，。！？；：)】]}>、\n"
        if len(current) >= target_chunk_size or boundary:
            yield current
            current = ""

    if current:
        yield current
