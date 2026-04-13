from __future__ import annotations

import atexit
import asyncio
import concurrent.futures
import json
import logging
import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.responses import StreamingResponse

from deerflow.models.deepseek_web_bridge import DeepSeekWebBridge

logger = logging.getLogger(__name__)

DEFAULT_URL = os.environ.get("DEEPSEEK_WEB_URL", "https://chat.deepseek.com/")
DEFAULT_HEADLESS = os.environ.get("DEEPSEEK_WEB_HEADLESS", "1") == "1"
DEFAULT_FORCE_NEW_CHAT = os.environ.get("DEEPSEEK_WEB_FORCE_NEW_CHAT", "0") == "1"
DEFAULT_MODEL_ID = os.environ.get("DEEPSEEK_LOCAL_MODEL", "deepseek-web-deerflow")


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    profile_dir: str
    force_new_chat: bool = DEFAULT_FORCE_NEW_CHAT
    sticky_marker: str | None = None
    sticky_reanchor_messages: int | None = 24
    session_state_path: str | None = None
    reuse_persisted_chat: bool = False


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
    "deepseek-web-cherry": ModelSpec(
        model_id="deepseek-web-cherry",
        profile_dir=os.environ.get("DEEPSEEK_WEB_PROFILE_CHERRY", "~/.deerflow/profile-cherry"),
        force_new_chat=os.environ.get("DEEPSEEK_WEB_FORCE_NEW_CHAT_CHERRY", "0") == "1",
    ),
}

# Optional legacy alias for older configs.
MODEL_ALIASES = {
    "deepseek-web": "deepseek-web-deerflow",
}

_bridges: dict[str, DeepSeekWebBridge] = {}
_playwright_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="deepseek-web")


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


def _bridge_cache_key(spec: ModelSpec) -> str:
    if spec.profile_dir == DEERFLOW_PROFILE_DIR and spec.session_state_path == DEERFLOW_SESSION_STATE_PATH:
        return "deepseek-web-deerflow-shared"
    return spec.model_id


def get_bridge(model_name: str) -> tuple[ModelSpec, DeepSeekWebBridge]:
    spec = get_model_spec(model_name)
    bridge = _bridges.get(_bridge_cache_key(spec))
    if bridge is None:
        bridge = DeepSeekWebBridge(
            url=DEFAULT_URL,
            user_data_dir=spec.profile_dir,
            headless=DEFAULT_HEADLESS,
            force_new_chat=spec.force_new_chat,
            sticky_marker=spec.sticky_marker,
            sticky_reanchor_messages=spec.sticky_reanchor_messages,
            session_state_path=spec.session_state_path,
            reuse_persisted_chat=spec.reuse_persisted_chat,
        )
        _bridges[_bridge_cache_key(spec)] = bridge
    return spec, bridge


def bridge_call_with_spec(
    bridge: DeepSeekWebBridge,
    *,
    spec: ModelSpec,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    include_debug: bool = False,
) -> dict[str, Any]:
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
        return bridge.call(
            messages=messages,
            tools=tools,
            include_debug=include_debug,
        )
    finally:
        bridge.force_new_chat = original_force_new_chat
        bridge.sticky_marker = original_sticky_marker
        bridge.sticky_reanchor_messages = original_sticky_reanchor_messages
        bridge.session_state_path = original_session_state_path
        bridge.reuse_persisted_chat = original_reuse_persisted_chat


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

        return normalized


class DebugTraceRequest(ChatCompletionRequest):
    include_payload: bool = False


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
    spec, bridge = get_bridge(request.model)
    resolved_model = spec.model_id
    request_id = uuid.uuid4().hex[:8]
    request_messages = [message.model_dump(exclude_none=True) for message in request.messages]
    request_tools = request.tools or []
    logger.warning(
        "provider[%s] /v1 start model=%s stream=%s messages=%d tools=%d",
        request_id,
        resolved_model,
        request.stream,
        len(request_messages),
        len(request_tools),
    )

    try:
        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(
            _playwright_executor,
            lambda: bridge_call_with_spec(
                bridge,
                spec=spec,
                messages=request_messages,
                tools=request_tools,
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

    message: dict[str, Any] = {
        "role": "assistant",
        "content": payload.get("content", ""),
    }
    tool_calls = payload.get("tool_calls") or []
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": tool_call["id"],
                "type": "function",
                "function": {
                    "name": tool_call["name"],
                    "arguments": tool_call["arguments"]
                    if isinstance(tool_call["arguments"], str)
                    else json.dumps(tool_call["arguments"], ensure_ascii=False),
                },
            }
            for tool_call in tool_calls
        ]
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

    finish_reason = "tool_calls" if tool_calls else "stop"

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
            }
        ],
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


@app.post("/debug/chat-timings")
async def debug_chat_timings(request: DebugTraceRequest) -> dict[str, Any]:
    spec, bridge = get_bridge(request.model)
    resolved_model = spec.model_id
    request_id = uuid.uuid4().hex[:8]
    request_messages = [message.model_dump(exclude_none=True) for message in request.messages]
    request_tools = request.tools or []
    logger.warning(
        "provider[%s] /debug start model=%s messages=%d tools=%d include_payload=%s",
        request_id,
        resolved_model,
        len(request_messages),
        len(request_tools),
        request.include_payload,
    )

    try:
        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(
            _playwright_executor,
            lambda: bridge_call_with_spec(
                bridge,
                spec=spec,
                messages=request_messages,
                tools=request_tools,
                include_debug=True,
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
            }
        ],
    }
    yield f"data: {json.dumps(initial_chunk, ensure_ascii=False)}\n\n"

    if tool_calls:
        tool_chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": tool_calls},
                    "finish_reason": None,
                }
            ],
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
                    }
                ],
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
            }
        ],
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
        }
        yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"

    yield "data: [DONE]\n\n"


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
