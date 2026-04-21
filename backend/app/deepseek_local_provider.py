from __future__ import annotations

import atexit
import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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


def _int_env(name: str, default: int, *, minimum: int = 1, maximum: int = 8) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid integer env %s=%r; using %d", name, raw, default)
        return default
    return max(minimum, min(maximum, value))


def _selector_tuple_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    selectors = tuple(selector.strip() for selector in raw.split("||") if selector.strip())
    return selectors or default


DEEPSEEK_WEB_POOL_SIZE = _int_env("DEEPSEEK_WEB_POOL_SIZE", 1, minimum=1, maximum=6)
DEEPSEEK_WEB_POOL_PROFILE_ROOT = os.environ.get(
    "DEEPSEEK_WEB_POOL_PROFILE_ROOT",
    "~/.deerflow/deepseek-web-profile-pool",
)
DEEPSEEK_WEB_POOL_QUEUE_LIMIT = _int_env(
    "DEEPSEEK_WEB_POOL_QUEUE_LIMIT",
    max(4, DEEPSEEK_WEB_POOL_SIZE * 2),
    minimum=0,
    maximum=64,
)
DEEPSEEK_WEB_POOL_ACQUIRE_TIMEOUT_S = _int_env(
    "DEEPSEEK_WEB_POOL_ACQUIRE_TIMEOUT_S",
    600,
    minimum=1,
    maximum=3600,
)
DEEPSEEK_WEB_PROTOCOL_RETRIES = _int_env(
    "DEEPSEEK_WEB_PROTOCOL_RETRIES",
    1,
    minimum=0,
    maximum=3,
)
PROVIDER_WEB_SEARCH_MAX_RESULTS = _int_env(
    "DEEPSEEK_PROVIDER_WEB_SEARCH_MAX_RESULTS",
    5,
    minimum=1,
    maximum=10,
)
PROVIDER_WEB_SEARCH_MAX_STEPS = _int_env(
    "DEEPSEEK_PROVIDER_WEB_SEARCH_MAX_STEPS",
    3,
    minimum=1,
    maximum=6,
)
DEFAULT_RESPONSES_STORE = os.environ.get("DEEPSEEK_LOCAL_RESPONSES_STORE", "1").strip().lower() not in {
    "0",
    "false",
    "off",
    "no",
    "",
}
DEFAULT_EXPERT_MODE_ENABLED = os.environ.get("DEEPSEEK_LOCAL_EXPERT_MODE", "1").strip().lower() not in {
    "0",
    "false",
    "off",
    "no",
    "",
}


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    profile_dir: str
    url: str = DEFAULT_URL
    headless: bool = DEFAULT_HEADLESS
    force_new_chat: bool = DEFAULT_FORCE_NEW_CHAT
    sticky_marker: str | None = None
    sticky_reanchor_messages: int | None = 24
    session_state_path: str | None = None
    reuse_persisted_chat: bool = False
    forced_thinking_enabled: bool | None = None
    forced_expert_mode_enabled: bool | None = None
    input_selectors: tuple[str, ...] | None = None
    send_selectors: tuple[str, ...] | None = None
    new_chat_selectors: tuple[str, ...] | None = None
    assistant_selectors: tuple[str, ...] | None = None
    preferred_model_label: str | None = None
    model_menu_selectors: tuple[str, ...] | None = None
    model_option_selectors: tuple[str, ...] | None = None
    page_load_timeout_ms: int | None = None
    response_timeout_ms: int | None = None
    stable_poll_interval_ms: int | None = None
    stable_rounds: int | None = None
    copy_probe_max_ms: int | None = None
    copy_candidate_max_distance: int | None = None
    fast_new_chat: bool = False


DEERFLOW_PROFILE_DIR = os.environ.get("DEEPSEEK_WEB_PROFILE_DEERFLOW", "~/.deerflow/profile-deerflow")
DEERFLOW_SESSION_STATE_PATH = os.environ.get(
    "DEEPSEEK_WEB_SESSION_STATE_DEERFLOW",
    "~/.deerflow/deepseek-web-deerflow-session.json",
)
DEERFLOW_FORCE_NEW_CHAT = os.environ.get("DEEPSEEK_WEB_FORCE_NEW_CHAT_DEERFLOW", "1") == "1"
DEERFLOW_STICKY_MARKER = os.environ.get("DEEPSEEK_WEB_STICKY_MARKER_DEERFLOW", "flowflow__system_prompt_v2")
DEERFLOW_STICKY_REANCHOR_MESSAGES = int(os.environ.get("DEEPSEEK_WEB_STICKY_REANCHOR_MESSAGES_DEERFLOW", "24"))

XIAOMI_MIMO_URL = os.environ.get("XIAOMI_MIMO_WEB_URL", "https://aistudio.xiaomimimo.com/#/c")
XIAOMI_MIMO_HEADLESS = os.environ.get(
    "XIAOMI_MIMO_WEB_HEADLESS",
    os.environ.get("DEEPSEEK_WEB_HEADLESS", "1"),
) == "1"
XIAOMI_MIMO_PROFILE_DIR = os.environ.get("XIAOMI_MIMO_WEB_PROFILE", "~/.deerflow/profile-xiaomi-mimo")
XIAOMI_MIMO_SESSION_STATE_PATH = os.environ.get(
    "XIAOMI_MIMO_WEB_SESSION_STATE",
    "~/.deerflow/xiaomi-mimo-session.json",
)
XIAOMI_MIMO_FORCE_NEW_CHAT = os.environ.get("XIAOMI_MIMO_FORCE_NEW_CHAT", "1") == "1"
XIAOMI_MIMO_STICKY_MARKER = os.environ.get("XIAOMI_MIMO_STICKY_MARKER", "mimo__system_prompt_v2")
XIAOMI_MIMO_STICKY_REANCHOR_MESSAGES = int(os.environ.get("XIAOMI_MIMO_STICKY_REANCHOR_MESSAGES", "24"))
XIAOMI_MIMO_MODEL_LABEL = os.environ.get("XIAOMI_MIMO_WEB_MODEL_LABEL", "MiMo-V2-Pro")
XIAOMI_MIMO_RESPONSE_TIMEOUT_MS = _int_env(
    "XIAOMI_MIMO_RESPONSE_TIMEOUT_MS",
    90_000,
    minimum=10_000,
    maximum=300_000,
)
XIAOMI_MIMO_STABLE_POLL_INTERVAL_MS = _int_env(
    "XIAOMI_MIMO_STABLE_POLL_INTERVAL_MS",
    800,
    minimum=100,
    maximum=3_000,
)
XIAOMI_MIMO_STABLE_ROUNDS = _int_env(
    "XIAOMI_MIMO_STABLE_ROUNDS",
    2,
    minimum=1,
    maximum=8,
)
XIAOMI_MIMO_COPY_PROBE_MAX_MS = _int_env(
    "XIAOMI_MIMO_COPY_PROBE_MAX_MS",
    350,
    minimum=0,
    maximum=3_000,
)
XIAOMI_MIMO_COPY_CANDIDATE_MAX_DISTANCE = _int_env(
    "XIAOMI_MIMO_COPY_CANDIDATE_MAX_DISTANCE",
    180,
    minimum=40,
    maximum=1_000,
)
XIAOMI_MIMO_FAST_NEW_CHAT = os.environ.get("XIAOMI_MIMO_FAST_NEW_CHAT", "1").strip().lower() not in {
    "0",
    "false",
    "off",
    "no",
    "",
}
XIAOMI_MIMO_INPUT_SELECTORS = _selector_tuple_env(
    "XIAOMI_MIMO_INPUT_SELECTORS",
    (
        'textarea[placeholder*="Sign in to continue chatting"]',
        'textarea[placeholder*="Message"]',
        'textarea[placeholder*="Ask"]',
        'textarea[placeholder*="发送"]',
        'textarea[placeholder*="输入"]',
        "textarea",
        '[contenteditable="true"]',
    ),
)
XIAOMI_MIMO_SEND_SELECTORS = _selector_tuple_env(
    "XIAOMI_MIMO_SEND_SELECTORS",
    (
        "button.rounded-full.h-7.w-7:not([disabled])",
        'button[aria-label*="Send" i]:not([disabled])',
        'button[aria-label*="发送"]:not([disabled])',
    ),
)
XIAOMI_MIMO_NEW_CHAT_SELECTORS = _selector_tuple_env(
    "XIAOMI_MIMO_NEW_CHAT_SELECTORS",
    (
        'button[aria-label="New conversation"]',
        'button:has-text("New conversation")',
        'button:has-text("新建对话")',
        'button:has-text("新对话")',
    ),
)
XIAOMI_MIMO_ASSISTANT_SELECTORS = _selector_tuple_env(
    "XIAOMI_MIMO_ASSISTANT_SELECTORS",
    (
        "#message-list .markdown-prose",
        '#message-list [class*="Markdown_markdown"]',
        '[data-message-author-role="assistant"]',
        '[data-role="assistant"]',
    ),
)
XIAOMI_MIMO_MODEL_MENU_SELECTORS = _selector_tuple_env(
    "XIAOMI_MIMO_MODEL_MENU_SELECTORS",
    (
        'nav div[class*="cursor-pointer"]:has-text("MiMo-V2")',
        'div[class*="cursor-pointer"]:has-text("MiMo-V2")',
        "text=/MiMo-V2-(Flash|Pro|Omni|TTS)/",
    ),
)
XIAOMI_MIMO_MODEL_OPTION_SELECTORS = _selector_tuple_env(
    "XIAOMI_MIMO_MODEL_OPTION_SELECTORS",
    (
        f'text="{XIAOMI_MIMO_MODEL_LABEL}"',
        f'div:has-text("{XIAOMI_MIMO_MODEL_LABEL}")',
    ),
)

MODEL_SPECS: dict[str, ModelSpec] = {
    "deepseek-web-deerflow": ModelSpec(
        model_id="deepseek-web-deerflow",
        profile_dir=DEERFLOW_PROFILE_DIR,
        force_new_chat=DEERFLOW_FORCE_NEW_CHAT,
        sticky_marker=DEERFLOW_STICKY_MARKER,
        sticky_reanchor_messages=DEERFLOW_STICKY_REANCHOR_MESSAGES,
        session_state_path=DEERFLOW_SESSION_STATE_PATH,
        forced_expert_mode_enabled=DEFAULT_EXPERT_MODE_ENABLED,
    ),
    "deepseek-web-deerflow-sticky": ModelSpec(
        model_id="deepseek-web-deerflow-sticky",
        profile_dir=DEERFLOW_PROFILE_DIR,
        force_new_chat=False,
        sticky_marker=DEERFLOW_STICKY_MARKER,
        sticky_reanchor_messages=DEERFLOW_STICKY_REANCHOR_MESSAGES,
        session_state_path=DEERFLOW_SESSION_STATE_PATH,
        reuse_persisted_chat=True,
        forced_expert_mode_enabled=DEFAULT_EXPERT_MODE_ENABLED,
    ),
    "DeepSeekV4": ModelSpec(
        model_id="DeepSeekV4",
        profile_dir=DEERFLOW_PROFILE_DIR,
        force_new_chat=DEERFLOW_FORCE_NEW_CHAT,
        sticky_marker=DEERFLOW_STICKY_MARKER,
        sticky_reanchor_messages=DEERFLOW_STICKY_REANCHOR_MESSAGES,
        session_state_path=DEERFLOW_SESSION_STATE_PATH,
        reuse_persisted_chat=False,
        forced_thinking_enabled=False,
        forced_expert_mode_enabled=DEFAULT_EXPERT_MODE_ENABLED,
    ),
    "DeepSeekV4-thinking": ModelSpec(
        model_id="DeepSeekV4-thinking",
        profile_dir=DEERFLOW_PROFILE_DIR,
        force_new_chat=DEERFLOW_FORCE_NEW_CHAT,
        sticky_marker=DEERFLOW_STICKY_MARKER,
        sticky_reanchor_messages=DEERFLOW_STICKY_REANCHOR_MESSAGES,
        session_state_path=DEERFLOW_SESSION_STATE_PATH,
        reuse_persisted_chat=False,
        forced_thinking_enabled=True,
        forced_expert_mode_enabled=DEFAULT_EXPERT_MODE_ENABLED,
    ),
    "xiaomi-mimo-v2-pro": ModelSpec(
        model_id="xiaomi-mimo-v2-pro",
        profile_dir=XIAOMI_MIMO_PROFILE_DIR,
        url=XIAOMI_MIMO_URL,
        headless=XIAOMI_MIMO_HEADLESS,
        force_new_chat=XIAOMI_MIMO_FORCE_NEW_CHAT,
        sticky_marker=XIAOMI_MIMO_STICKY_MARKER,
        sticky_reanchor_messages=XIAOMI_MIMO_STICKY_REANCHOR_MESSAGES,
        session_state_path=XIAOMI_MIMO_SESSION_STATE_PATH,
        reuse_persisted_chat=False,
        input_selectors=XIAOMI_MIMO_INPUT_SELECTORS,
        send_selectors=XIAOMI_MIMO_SEND_SELECTORS,
        new_chat_selectors=XIAOMI_MIMO_NEW_CHAT_SELECTORS,
        assistant_selectors=XIAOMI_MIMO_ASSISTANT_SELECTORS,
        preferred_model_label=XIAOMI_MIMO_MODEL_LABEL,
        model_menu_selectors=XIAOMI_MIMO_MODEL_MENU_SELECTORS,
        model_option_selectors=XIAOMI_MIMO_MODEL_OPTION_SELECTORS,
        response_timeout_ms=XIAOMI_MIMO_RESPONSE_TIMEOUT_MS,
        stable_poll_interval_ms=XIAOMI_MIMO_STABLE_POLL_INTERVAL_MS,
        stable_rounds=XIAOMI_MIMO_STABLE_ROUNDS,
        copy_probe_max_ms=XIAOMI_MIMO_COPY_PROBE_MAX_MS,
        copy_candidate_max_distance=XIAOMI_MIMO_COPY_CANDIDATE_MAX_DISTANCE,
        fast_new_chat=XIAOMI_MIMO_FAST_NEW_CHAT,
    ),
}

# Optional legacy alias for older configs.
MODEL_ALIASES = {
    "deepseek-web": "DeepSeekV4",
    "DeepSeek V4": "DeepSeekV4",
    "DeepSeek V4-thinking": "DeepSeekV4-thinking",
    "DeepSeekV3": "DeepSeekV4",
    "DeepSeekV3-thinking": "DeepSeekV4-thinking",
    "mimo": "xiaomi-mimo-v2-pro",
    "mimo-pro": "xiaomi-mimo-v2-pro",
    "mimo-v2-pro": "xiaomi-mimo-v2-pro",
    "MiMo-V2-Pro": "xiaomi-mimo-v2-pro",
    "MIMO V2 PRO": "xiaomi-mimo-v2-pro",
    "xiaomi": "xiaomi-mimo-v2-pro",
    "xiaomi-mimo": "xiaomi-mimo-v2-pro",
    "Xiaomi MiMo-V2-Pro": "xiaomi-mimo-v2-pro",
}


def is_interface_enabled(name: str) -> bool:
    mode = INTERFACE_MODE or "both"
    if mode not in {"openai", "anthropic", "both"}:
        mode = "both"
    return mode == "both" or mode == name

_bridge_pools: dict[str, "BridgePool"] = {}
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
    if (
        spec.url == DEFAULT_URL
        and spec.profile_dir == DEERFLOW_PROFILE_DIR
        and spec.session_state_path == DEERFLOW_SESSION_STATE_PATH
    ):
        return "deepseek-web-deerflow-shared"
    return f"{spec.model_id}:{spec.url}:{spec.profile_dir}:{spec.session_state_path or ''}"


def _safe_pool_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return normalized or "default"


def _pooled_profile_dir(base_profile_dir: str, *, cache_key: str, slot_index: int, pool_size: int) -> str:
    base_path = Path(base_profile_dir).expanduser()
    if pool_size <= 1 or slot_index == 0:
        return str(base_path)

    pool_root = Path(DEEPSEEK_WEB_POOL_PROFILE_ROOT).expanduser()
    slot_path = pool_root / _safe_pool_name(cache_key) / f"profile-{slot_index}"
    if slot_path.exists():
        return str(slot_path)

    slot_path.parent.mkdir(parents=True, exist_ok=True)
    if base_path.exists():
        logger.warning(
            "Creating DeepSeek web profile clone slot=%d source=%s target=%s",
            slot_index,
            base_path,
            slot_path,
        )
        shutil.copytree(
            base_path,
            slot_path,
            symlinks=True,
            ignore=shutil.ignore_patterns(
                "Singleton*",
                "LOCK",
                "lockfile",
                "Crashpad",
                "GPUCache",
                "GrShaderCache",
                "ShaderCache",
                "Code Cache",
                "Cache",
            ),
        )
    else:
        slot_path.mkdir(parents=True, exist_ok=True)
    return str(slot_path)


def _pooled_session_state_path(base_state_path: str | None, *, slot_index: int, pool_size: int) -> str | None:
    if not base_state_path or pool_size <= 1:
        return base_state_path
    path = Path(base_state_path).expanduser()
    suffix = path.suffix or ".json"
    stem = path.stem if path.suffix else path.name
    return str(path.with_name(f"{stem}--pool-{slot_index}{suffix}"))


def _effective_pool_size(base_spec: ModelSpec) -> int:
    if base_spec.reuse_persisted_chat:
        return 1
    return DEEPSEEK_WEB_POOL_SIZE


def _make_bridge(base_spec: ModelSpec, *, cache_key: str, slot_index: int, pool_size: int) -> DeepSeekWebBridge:
    bridge_kwargs: dict[str, Any] = {
        "url": base_spec.url,
        "user_data_dir": _pooled_profile_dir(
            base_spec.profile_dir,
            cache_key=cache_key,
            slot_index=slot_index,
            pool_size=pool_size,
        ),
        "headless": base_spec.headless,
        "force_new_chat": base_spec.force_new_chat,
        "sticky_marker": base_spec.sticky_marker,
        "sticky_reanchor_messages": base_spec.sticky_reanchor_messages,
        "session_state_path": _pooled_session_state_path(
            base_spec.session_state_path,
            slot_index=slot_index,
            pool_size=pool_size,
        ),
        "reuse_persisted_chat": base_spec.reuse_persisted_chat,
        "fast_new_chat": base_spec.fast_new_chat,
    }
    for attr_name in (
        "page_load_timeout_ms",
        "response_timeout_ms",
        "stable_poll_interval_ms",
        "stable_rounds",
        "copy_probe_max_ms",
        "copy_candidate_max_distance",
    ):
        attr_value = getattr(base_spec, attr_name)
        if attr_value is not None:
            bridge_kwargs[attr_name] = attr_value
    for attr_name in (
        "input_selectors",
        "send_selectors",
        "new_chat_selectors",
        "assistant_selectors",
        "preferred_model_label",
        "model_menu_selectors",
        "model_option_selectors",
    ):
        attr_value = getattr(base_spec, attr_name)
        if attr_value is not None:
            bridge_kwargs[attr_name] = attr_value
    return DeepSeekWebBridge(**bridge_kwargs)


class BridgePoolBusy(RuntimeError):
    pass


class BridgeSlot:
    def __init__(self, *, cache_key: str, base_spec: ModelSpec, slot_index: int, pool_size: int) -> None:
        self.index = slot_index
        self.pool_size = pool_size
        self.bridge = _make_bridge(
            base_spec,
            cache_key=cache_key,
            slot_index=slot_index,
            pool_size=pool_size,
        )
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"deepseek-web-{_safe_pool_name(cache_key)}-{slot_index}",
        )

    def close(self) -> None:
        try:
            future = self.executor.submit(lambda: _run_in_playwright_worker(self.bridge.close))
            future.result(timeout=30)
        except Exception:
            logger.debug("Failed to close DeepSeek bridge slot %d cleanly.", self.index, exc_info=True)
        self.executor.shutdown(wait=False, cancel_futures=True)


class BridgePool:
    def __init__(self, *, cache_key: str, base_spec: ModelSpec, size: int) -> None:
        self.cache_key = cache_key
        self.size = max(1, size)
        self.queue_limit = max(0, DEEPSEEK_WEB_POOL_QUEUE_LIMIT)
        self._all: list[BridgeSlot] = [
            BridgeSlot(cache_key=cache_key, base_spec=base_spec, slot_index=index, pool_size=self.size)
            for index in range(self.size)
        ]
        self._available: queue.LifoQueue[BridgeSlot] = queue.LifoQueue()
        self._admission = threading.BoundedSemaphore(self.size + self.queue_limit)
        for slot in reversed(self._all):
            self._available.put(slot)

    def acquire(self) -> BridgeSlot:
        admitted = self._admission.acquire(blocking=False)
        if not admitted:
            raise BridgePoolBusy(
                f"DeepSeek web bridge pool is busy: size={self.size} queue_limit={self.queue_limit}"
            )
        try:
            return self._available.get(timeout=DEEPSEEK_WEB_POOL_ACQUIRE_TIMEOUT_S)
        except queue.Empty as exc:
            self._admission.release()
            raise BridgePoolBusy(
                f"Timed out waiting for DeepSeek web bridge slot after {DEEPSEEK_WEB_POOL_ACQUIRE_TIMEOUT_S}s"
            ) from exc

    def release(self, slot: BridgeSlot) -> None:
        self._available.put(slot)
        self._admission.release()

    def first_bridge(self) -> DeepSeekWebBridge:
        return self._all[0].bridge

    def close(self) -> None:
        for slot in self._all:
            slot.close()


def get_bridge_pool(model_name: str, request_user: str | None = None) -> tuple[ModelSpec, BridgePool]:
    base_spec = get_model_spec(model_name)
    spec = resolve_request_spec(model_name, request_user)
    cache_key = _bridge_cache_key(base_spec)
    pool = _bridge_pools.get(cache_key)
    if pool is None:
        pool = BridgePool(
            cache_key=cache_key,
            base_spec=base_spec,
            size=_effective_pool_size(base_spec),
        )
        _bridge_pools[cache_key] = pool
        logger.warning(
            "DeepSeek bridge pool initialized key=%s size=%d queue_limit=%d",
            cache_key,
            pool.size,
            pool.queue_limit,
        )
    return spec, pool


def get_bridge(model_name: str, request_user: str | None = None) -> tuple[ModelSpec, DeepSeekWebBridge]:
    spec, pool = get_bridge_pool(model_name, request_user)
    return spec, pool.first_bridge()


def _should_retry_protocol_payload(payload: dict[str, Any], *, output_protocol: str) -> bool:
    if output_protocol not in {"openai", "anthropic"}:
        return False
    parse_error = payload.get("parse_error")
    return parse_error in {
        "invalid_json",
        "prompt_replay",
        "placeholder_payload",
        "empty_payload",
        "low_signal_payload",
    }


def _with_protocol_retry_message(
    messages: list[dict[str, Any]],
    *,
    payload: dict[str, Any],
    attempt: int,
) -> list[dict[str, Any]]:
    raw_text = payload.get("raw_text") if isinstance(payload, dict) else ""
    preview = str(raw_text or payload.get("content", ""))[:600]
    retry_hint = (
        "【非常重要：上一轮输出格式错误，系统无法解析。请重新执行上一条请求，不要解释错误原因。】\n"
        "你必须只输出一个 JSON 对象，不能输出普通聊天文字、Markdown、代码块、XML 或 <tool_call> 标签。\n"
        '唯一允许的顶层结构是：{"content":"string","tool_calls":[{"name":"string","arguments":{},"id":"string"}]}\n'
        "需要调用工具时，必须把工具调用放入 tool_calls；arguments 必须是 JSON 对象；id 必须是非空字符串。\n"
        "不需要工具时，tool_calls 必须是 []。\n"
        f"这是第 {attempt} 次协议重试。上一轮非法输出预览如下，仅用于纠正格式，不要复述：\n"
        f"{preview}"
    )
    return [dict(message) for message in messages] + [{"role": "user", "content": retry_hint}]


def bridge_call_with_spec(
    bridge: DeepSeekWebBridge,
    *,
    spec: ModelSpec,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    thinking_enabled: bool | None = None,
    expert_mode_enabled: bool | None = None,
    include_debug: bool = False,
    output_protocol: str = "openai",
) -> dict[str, Any]:
    def operation() -> dict[str, Any]:
        retry_messages = messages
        payload = _bridge_call_compat(
            bridge,
            messages=retry_messages,
            tools=tools,
            thinking_enabled=thinking_enabled,
            expert_mode_enabled=expert_mode_enabled,
            include_debug=include_debug,
            output_protocol=output_protocol,
        )
        for attempt in range(1, DEEPSEEK_WEB_PROTOCOL_RETRIES + 1):
            if not _should_retry_protocol_payload(payload, output_protocol=output_protocol):
                return payload
            logger.warning(
                "provider bridge protocol retry attempt=%d parse_error=%s raw_preview=%r",
                attempt,
                payload.get("parse_error"),
                str(payload.get("raw_text") or payload.get("content") or "")[:300],
            )
            retry_messages = _with_protocol_retry_message(
                messages,
                payload=payload,
                attempt=attempt,
            )
            payload = _bridge_call_compat(
                bridge,
                messages=retry_messages,
                tools=tools,
                thinking_enabled=thinking_enabled,
                expert_mode_enabled=expert_mode_enabled,
                include_debug=include_debug,
                output_protocol=output_protocol,
            )
            payload["protocol_retry_count"] = attempt
        return payload

    return run_bridge_with_spec(
        bridge,
        spec=spec,
        operation=operation,
    )


def _bridge_call_compat(
    bridge: DeepSeekWebBridge,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    thinking_enabled: bool | None,
    expert_mode_enabled: bool | None,
    include_debug: bool,
    output_protocol: str,
) -> dict[str, Any]:
    attempts = [
        {
            "messages": messages,
            "tools": tools,
            "thinking_enabled": thinking_enabled,
            "expert_mode_enabled": expert_mode_enabled,
            "include_debug": include_debug,
            "output_protocol": output_protocol,
        },
        {
            "messages": messages,
            "tools": tools,
            "thinking_enabled": thinking_enabled,
            "expert_mode_enabled": expert_mode_enabled,
            "include_debug": include_debug,
        },
        {
            "messages": messages,
            "tools": tools,
            "thinking_enabled": thinking_enabled,
            "include_debug": include_debug,
            "output_protocol": output_protocol,
        },
        {
            "messages": messages,
            "tools": tools,
            "thinking_enabled": thinking_enabled,
            "include_debug": include_debug,
        },
    ]
    last_exc: TypeError | None = None
    for kwargs in attempts:
        try:
            return bridge.call(**kwargs)
        except TypeError as exc:
            message = str(exc)
            if "output_protocol" in message or "expert_mode_enabled" in message:
                last_exc = exc
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("bridge.call compatibility dispatch failed without a captured TypeError")


def _run_in_playwright_worker(operation):
    # Some callers can leave an event loop bound to the worker thread.
    # Playwright Sync API rejects that environment, so detach it explicitly.
    try:
        asyncio.set_event_loop(None)
    except Exception:
        pass
    return operation()


def _spec_for_bridge_slot(spec: ModelSpec, slot: BridgeSlot) -> ModelSpec:
    return replace(
        spec,
        profile_dir=slot.bridge.user_data_dir,
        session_state_path=_pooled_session_state_path(
            spec.session_state_path,
            slot_index=slot.index,
            pool_size=slot.pool_size,
        ),
    )


async def run_on_bridge_slot(
    pool: BridgePool,
    *,
    spec: ModelSpec,
    request_id: str,
    route: str,
    operation,
):
    try:
        slot = await asyncio.to_thread(pool.acquire)
    except BridgePoolBusy as exc:
        logger.warning("provider[%s] %s bridge pool busy: %s", request_id, route, exc)
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    slot_spec = _spec_for_bridge_slot(spec, slot)
    logger.warning(
        "provider[%s] %s acquired bridge slot=%d pool=%s available=%d",
        request_id,
        route,
        slot.index,
        pool.cache_key,
        pool._available.qsize(),
    )
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            slot.executor,
            lambda: _run_in_playwright_worker(lambda: operation(slot.bridge, slot_spec)),
        )
    except Exception:
        logger.warning(
            "provider[%s] %s resetting failed bridge slot=%d pool=%s",
            request_id,
            route,
            slot.index,
            pool.cache_key,
            exc_info=True,
        )
        try:
            await loop.run_in_executor(
                slot.executor,
                lambda: _run_in_playwright_worker(slot.bridge.close),
            )
        except Exception:
            logger.debug(
                "provider[%s] %s failed to reset bridge slot=%d",
                request_id,
                route,
                slot.index,
                exc_info=True,
            )
        raise
    finally:
        pool.release(slot)
        logger.warning(
            "provider[%s] %s released bridge slot=%d pool=%s available=%d",
            request_id,
            route,
            slot.index,
            pool.cache_key,
            pool._available.qsize(),
        )


def close_bridges() -> None:
    for pool in _bridge_pools.values():
        pool.close()
    _bridge_pools.clear()


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
    expert_mode_enabled: bool | None = None
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


class ResponsesRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = Field(default=DEFAULT_MODEL_ID)
    input: Any = ""
    instructions: str | None = None
    include: list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    stream: bool = False
    user: str | None = None
    thinking_enabled: bool | None = None
    expert_mode_enabled: bool | None = None
    reasoning: dict[str, Any] | None = None
    store: bool | None = None
    prompt_cache_retention: str | None = None


class DebugTraceRequest(ChatCompletionRequest):
    include_payload: bool = False


class ThinkingModeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = Field(default=DEFAULT_MODEL_ID)
    user: str | None = None
    thinking_enabled: bool | None = None
    visible: bool = False


class ExpertModeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = Field(default=DEFAULT_MODEL_ID)
    user: str | None = None
    expert_mode_enabled: bool | None = None
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
    expert_mode_enabled: bool | None = None
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


def resolve_request_expert_mode_enabled(request: ChatCompletionRequest) -> bool | None:
    if isinstance(getattr(request, "expert_mode_enabled", None), bool):
        return request.expert_mode_enabled
    extra_body = request.extra_body or {}
    for key in ("expert_mode_enabled", "expert_mode"):
        candidate = extra_body.get(key)
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


def resolve_effective_expert_mode_enabled(
    requested_expert_mode_enabled: bool | None,
    *,
    spec: ModelSpec,
) -> bool | None:
    if isinstance(spec.forced_expert_mode_enabled, bool):
        return spec.forced_expert_mode_enabled
    return requested_expert_mode_enabled


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


RESPONSES_TOOL_CALLING_HINT = (
    "Responses compatibility rule: when you need to call any tool, emit a real function_call/tool_call "
    "with a concrete non-empty call_id/id such as call_1. Do not return prose or empty JSON instead of "
    "the tool call."
)
RESPONSES_WEB_SEARCH_TOOL_TYPES = {"web_search", "web_search_preview"}
PROVIDER_WEB_SEARCH_TOOL_NAME = "web_search"
PROVIDER_WEB_SEARCH_TOOL_HINT = (
    "When the user needs current or external information, call the function tool "
    f'"{PROVIDER_WEB_SEARCH_TOOL_NAME}" with a concrete query instead of answering from memory. '
    "After tool results arrive, use them and cite the listed sources."
)
def normalize_responses_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            text = normalize_responses_text(item)
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        block_type = value.get("type")
        if block_type in {"input_text", "output_text", "text"}:
            text = value.get("text")
            return text if isinstance(text, str) else ""
        if block_type == "input_image":
            return "[image]"
        if "content" in value:
            return normalize_responses_text(value.get("content"))
        if "output" in value:
            return normalize_responses_text(value.get("output"))
        if "text" in value:
            return normalize_responses_text(value.get("text"))
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def responses_tools_to_openai_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function":
            continue
        function = tool.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str) and name:
                out.append(tool)
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
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                },
            }
        )
    return out


def split_responses_tools(
    tools: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    function_tools: list[dict[str, Any]] = []
    web_search_tools: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        tool_type = tool.get("type")
        if tool_type == "function":
            function_tools.extend(responses_tools_to_openai_tools([tool]))
        elif tool_type in RESPONSES_WEB_SEARCH_TOOL_TYPES:
            web_search_tools.append(tool)
    return function_tools, web_search_tools


def _append_system_hint(messages: list[dict[str, Any]], hint: str) -> list[dict[str, Any]]:
    if not hint:
        return messages
    if messages and messages[0].get("role") == "system":
        updated = dict(messages[0])
        existing = str(updated.get("content", "") or "").strip()
        updated["content"] = f"{existing}\n\n{hint}" if existing else hint
        return [updated, *messages[1:]]
    return [{"role": "system", "content": hint}, *messages]


def _response_include_has(request: ResponsesRequest, value: str) -> bool:
    include = request.include or []
    return any(isinstance(item, str) and item == value for item in include)


def _extract_allowed_domains(web_search_tools: list[dict[str, Any]]) -> list[str]:
    domains: list[str] = []
    for tool in web_search_tools:
        filters = tool.get("filters")
        candidates: Any = None
        if isinstance(filters, dict):
            candidates = filters.get("allowed_domains") or filters.get("domains")
        if candidates is None:
            candidates = tool.get("allowed_domains") or tool.get("domains")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            normalized = candidate.strip().lower()
            if normalized:
                domains.append(normalized)
    return list(dict.fromkeys(domains))


def _extract_max_results(web_search_tools: list[dict[str, Any]]) -> int:
    for tool in web_search_tools:
        for key in ("max_results", "limit"):
            value = tool.get(key)
            if isinstance(value, int) and value > 0:
                return min(10, value)
    return PROVIDER_WEB_SEARCH_MAX_RESULTS


def build_provider_web_search_tool(web_search_tools: list[dict[str, Any]]) -> dict[str, Any]:
    allowed_domains = _extract_allowed_domains(web_search_tools)
    domain_hint = ""
    if allowed_domains:
        domain_hint = f" Restrict results to these domains when possible: {', '.join(allowed_domains)}."
    return {
        "type": "function",
        "function": {
            "name": PROVIDER_WEB_SEARCH_TOOL_NAME,
            "description": (
                "Search the public web for up-to-date information and return JSON results with titles, URLs, and snippets."
                f"{domain_hint}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The exact web search query to run.",
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


def _normalize_tool_call_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _latest_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""

def _domain_allowed(url: str, allowed_domains: list[str]) -> bool:
    if not allowed_domains:
        return True
    hostname = (urlparse(url).hostname or "").lower().strip(".")
    if not hostname:
        return False
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in allowed_domains)


def perform_provider_web_search(
    query: str,
    *,
    max_results: int,
    allowed_domains: list[str],
) -> dict[str, Any]:
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise RuntimeError("ddgs is not installed in the provider environment.") from exc

    normalized_query = (query or "").strip()
    if not normalized_query:
        return {"query": "", "total_results": 0, "results": []}

    ddgs = DDGS(timeout=30)
    raw_results = ddgs.text(
        normalized_query,
        region="wt-wt",
        safesearch="moderate",
        max_results=max_results * 3 if allowed_domains else max_results,
    )
    normalized_results: list[dict[str, Any]] = []
    for item in raw_results or []:
        url = str(item.get("href") or item.get("link") or "").strip()
        if not url or not _domain_allowed(url, allowed_domains):
            continue
        normalized_results.append(
            {
                "title": str(item.get("title") or "").strip(),
                "url": url,
                "content": str(item.get("body") or item.get("snippet") or "").strip(),
            }
        )
        if len(normalized_results) >= max_results:
            break
    return {
        "query": normalized_query,
        "total_results": len(normalized_results),
        "results": normalized_results,
    }


def build_provider_web_search_item(
    search_result: dict[str, Any],
    *,
    include_sources: bool,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "type": "search",
        "query": search_result.get("query", ""),
        "queries": [search_result.get("query", "")],
    }
    if include_sources:
        action["sources"] = [
            {
                "type": "url",
                "title": result.get("title", ""),
                "url": result.get("url", ""),
                "snippet": result.get("content", ""),
            }
            for result in search_result.get("results", [])
        ]
    return {
        "id": f"ws_{uuid.uuid4().hex}",
        "type": "web_search_call",
        "status": "completed",
        "action": action,
    }


def _dedupe_search_results(search_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for search_result in search_results:
        for item in search_result.get("results", []):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            deduped.append(item)
    return deduped


def append_provider_search_citations(
    content: str,
    search_results: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    deduped = _dedupe_search_results(search_results)
    if not deduped:
        return content, []
    base_content = content.rstrip()
    separator = "\n\n" if base_content else ""
    prefix = "Sources:\n"
    lines: list[str] = []
    annotations: list[dict[str, Any]] = []
    current_offset = len(base_content) + len(separator) + len(prefix)
    for index, item in enumerate(deduped, start=1):
        title = str(item.get("title") or item.get("url") or f"Result {index}")
        url = str(item.get("url") or "").strip()
        line = f"[{index}] {title} - {url}"
        title_start = current_offset + len(f"[{index}] ")
        title_end = title_start + len(title)
        annotations.append(
            {
                "type": "url_citation",
                "start_index": title_start,
                "end_index": title_end,
                "title": title,
                "url": url,
            }
        )
        lines.append(line)
        current_offset += len(line) + 1
    return f"{base_content}{separator}{prefix}" + "\n".join(lines), annotations


def _filter_internal_web_search_calls(payload: dict[str, Any]) -> dict[str, Any]:
    tool_calls = payload.get("tool_calls")
    if not isinstance(tool_calls, list):
        return payload
    filtered = [
        tool_call
        for tool_call in tool_calls
        if not (isinstance(tool_call, dict) and tool_call.get("name") == PROVIDER_WEB_SEARCH_TOOL_NAME)
    ]
    if len(filtered) == len(tool_calls):
        return payload
    updated = dict(payload)
    updated["tool_calls"] = filtered
    return updated


def responses_input_to_chat_messages(input_value: Any, instructions: str | None, has_tools: bool) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    system_parts = [part for part in (instructions, RESPONSES_TOOL_CALLING_HINT if has_tools else None) if part]
    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})

    if isinstance(input_value, str):
        if input_value:
            messages.append({"role": "user", "content": input_value})
        return messages

    if isinstance(input_value, dict):
        items = [input_value]
    elif isinstance(input_value, list):
        items = input_value
    else:
        text = normalize_responses_text(input_value)
        if text:
            messages.append({"role": "user", "content": text})
        return messages

    for item in items:
        if isinstance(item, str):
            if item:
                messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            text = normalize_responses_text(item)
            if text:
                messages.append({"role": "user", "content": text})
            continue

        item_type = item.get("type")
        if item_type == "function_call":
            name = item.get("name")
            if not isinstance(name, str) or not name:
                continue
            arguments = item.get("arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments if isinstance(arguments, dict) else {}, ensure_ascii=False)
            call_id = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}"
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": str(call_id),
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": arguments,
                            },
                        }
                    ],
                }
            )
            continue

        if item_type == "function_call_output":
            call_id = item.get("call_id") or item.get("id")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call_id) if call_id else "",
                    "content": normalize_responses_text(item.get("output", "")),
                }
            )
            continue

        if item_type == "message" or "role" in item:
            role = item.get("role")
            if role not in {"system", "user", "assistant", "tool"}:
                role = "user"
            message: dict[str, Any] = {
                "role": role,
                "content": normalize_responses_text(item.get("content", "")),
            }
            if role == "tool":
                call_id = item.get("tool_call_id") or item.get("call_id")
                if call_id:
                    message["tool_call_id"] = str(call_id)
            tool_calls = item.get("tool_calls")
            if isinstance(tool_calls, list):
                message["tool_calls"] = tool_calls
            messages.append(message)
            continue

        text = normalize_responses_text(item.get("content", item.get("text", "")))
        if text:
            messages.append({"role": "user", "content": text})

    return messages


def resolve_responses_thinking_enabled(request: ResponsesRequest) -> bool | None:
    if request.thinking_enabled is not None:
        return request.thinking_enabled
    reasoning = request.reasoning if isinstance(request.reasoning, dict) else {}
    effort = reasoning.get("effort")
    if effort == "none":
        return False
    return None


def resolve_responses_expert_mode_enabled(request: ResponsesRequest) -> bool | None:
    if isinstance(request.expert_mode_enabled, bool):
        return request.expert_mode_enabled
    return None


async def resolve_provider_web_search(
    *,
    payload: dict[str, Any],
    pool: BridgePool,
    spec: ModelSpec,
    request_id: str,
    request_messages: list[dict[str, Any]],
    bridge_tools: list[dict[str, Any]],
    request_thinking_enabled: bool | None,
    request_expert_mode_enabled: bool | None,
    web_search_tools: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    if not web_search_tools:
        return payload, [], []

    request_messages = list(request_messages)
    max_results = _extract_max_results(web_search_tools)
    allowed_domains = _extract_allowed_domains(web_search_tools)
    include_sources = True
    web_search_items: list[dict[str, Any]] = []
    search_results: list[dict[str, Any]] = []

    for step in range(PROVIDER_WEB_SEARCH_MAX_STEPS):
        current_payload = apply_text_tool_call_fallback(payload, bridge_tools)
        current_payload = validate_tool_calls_against_schemas(current_payload, bridge_tools)
        raw_tool_calls = current_payload.get("tool_calls") or []
        provider_calls = [
            tool_call
            for tool_call in raw_tool_calls
            if isinstance(tool_call, dict) and tool_call.get("name") == PROVIDER_WEB_SEARCH_TOOL_NAME
        ]
        non_provider_tool_calls = [
            tool_call
            for tool_call in raw_tool_calls
            if isinstance(tool_call, dict) and tool_call.get("name") != PROVIDER_WEB_SEARCH_TOOL_NAME
        ]
        synthesized_provider_call = False

        if not provider_calls and step == 0 and not non_provider_tool_calls:
            fallback_query = _latest_user_text(request_messages)
            if fallback_query:
                provider_calls = [
                    {
                        "id": f"call_{uuid.uuid4().hex}",
                        "name": PROVIDER_WEB_SEARCH_TOOL_NAME,
                        "arguments": {"query": fallback_query},
                    }
                ]
                synthesized_provider_call = True

        if not provider_calls:
            return _filter_internal_web_search_calls(current_payload), web_search_items, search_results

        assistant_tool_calls: list[dict[str, Any]] = []
        tool_messages: list[dict[str, Any]] = []
        for tool_call in provider_calls:
            tool_call_id = str(tool_call.get("id") or f"call_{uuid.uuid4().hex}")
            arguments = _normalize_tool_call_arguments(tool_call.get("arguments"))
            query = str(arguments.get("query") or "").strip() or _latest_user_text(request_messages)
            if not query:
                continue
            search_result = await asyncio.to_thread(
                perform_provider_web_search,
                query,
                max_results=max_results,
                allowed_domains=allowed_domains,
            )
            search_results.append(search_result)
            web_search_items.append(
                build_provider_web_search_item(search_result, include_sources=include_sources)
            )
            assistant_tool_calls.append(
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": PROVIDER_WEB_SEARCH_TOOL_NAME,
                        "arguments": json.dumps({"query": query}, ensure_ascii=False),
                    },
                }
            )
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps(search_result, ensure_ascii=False),
                }
            )

        if not assistant_tool_calls or not tool_messages:
            return _filter_internal_web_search_calls(current_payload), web_search_items, search_results

        request_messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "" if synthesized_provider_call else (current_payload.get("content", "") or ""),
                    "tool_calls": assistant_tool_calls,
                },
                *tool_messages,
            ]
        )

        payload = await run_on_bridge_slot(
            pool,
            spec=spec,
            request_id=request_id,
            route="/v1/responses/web-search",
            operation=lambda bridge, slot_spec: bridge_call_with_spec(
                bridge,
                spec=slot_spec,
                messages=request_messages,
                tools=bridge_tools,
                thinking_enabled=request_thinking_enabled,
                expert_mode_enabled=request_expert_mode_enabled,
                output_protocol="openai",
            ),
        )

    return _filter_internal_web_search_calls(payload), web_search_items, search_results


def build_responses_body(
    *,
    model: str,
    content: str,
    tool_calls: list[dict[str, Any]],
    request: ResponsesRequest,
    extra_output_items: list[dict[str, Any]] | None = None,
    annotations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    output: list[dict[str, Any]] = list(extra_output_items or [])
    if content.strip():
        output.append(
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": content,
                        "annotations": annotations or [],
                    }
                ],
            }
        )

    for tool_call in tool_calls:
        arguments = tool_call.get("arguments", {})
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments if isinstance(arguments, dict) else {}, ensure_ascii=False)
        output.append(
            {
                "id": f"fc_{uuid.uuid4().hex}",
                "type": "function_call",
                "status": "completed",
                "call_id": tool_call.get("id") or f"call_{uuid.uuid4().hex}",
                "name": tool_call.get("name", ""),
                "arguments": arguments,
            }
        )

    if not output:
        output.append(
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "", "annotations": annotations or []}],
            }
        )

    usage = {
        "input_tokens": 0,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 0,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 0,
    }
    return {
        "id": f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "background": False,
        "error": None,
        "incomplete_details": None,
        "instructions": request.instructions,
        "max_output_tokens": None,
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": request.reasoning or {"effort": "none"},
        "store": bool(request.store) if request.store is not None else DEFAULT_RESPONSES_STORE,
        "temperature": None,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": request.tools or [],
        "top_p": None,
        "truncation": "disabled",
        "usage": usage,
        "user": request.user,
    }


def encode_response_sse(event_type: str, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event_type}\ndata: {data}\n\n"


async def stream_responses_events(response_body: dict[str, Any]) -> AsyncIterator[str]:
    created_response = dict(response_body)
    created_response["status"] = "in_progress"
    created_response["output"] = []
    yield encode_response_sse(
        "response.created",
        {"type": "response.created", "response": created_response},
    )
    for index, item in enumerate(response_body.get("output", [])):
        yield encode_response_sse(
            "response.output_item.added",
            {"type": "response.output_item.added", "output_index": index, "item": item},
        )
        yield encode_response_sse(
            "response.output_item.done",
            {"type": "response.output_item.done", "output_index": index, "item": item},
        )
    yield encode_response_sse(
        "response.completed",
        {"type": "response.completed", "response": response_body},
    )
    yield "data: [DONE]\n\n"


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


def _schema_allows_null(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    schema_type = schema.get("type")
    if schema_type == "null":
        return True
    if isinstance(schema_type, list) and "null" in schema_type:
        return True
    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if isinstance(variants, list) and any(_schema_allows_null(variant) for variant in variants):
            return True
    return False


def _fill_nullable_required_defaults(value: Any, schema: Any) -> tuple[Any, bool]:
    if not isinstance(schema, dict):
        return value, False

    schema_type = schema.get("type")
    if schema_type == "object" and isinstance(value, dict):
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return value, False
        updated = dict(value)
        rewritten = False
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                prop_schema = properties.get(key)
                if key not in updated and _schema_allows_null(prop_schema):
                    updated[key] = None
                    rewritten = True
        for key, prop_schema in properties.items():
            if key not in updated:
                continue
            nested_value, nested_rewritten = _fill_nullable_required_defaults(updated[key], prop_schema)
            if nested_rewritten:
                updated[key] = nested_value
                rewritten = True
        return updated, rewritten

    if schema_type == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if not isinstance(item_schema, dict):
            return value, False
        updated_items = []
        rewritten = False
        for item in value:
            nested_value, nested_rewritten = _fill_nullable_required_defaults(item, item_schema)
            updated_items.append(nested_value)
            rewritten = rewritten or nested_rewritten
        return updated_items, rewritten

    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if not isinstance(variants, list):
            continue
        for variant in variants:
            nested_value, nested_rewritten = _fill_nullable_required_defaults(value, variant)
            if nested_rewritten:
                return nested_value, True

    return value, False


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

        normalized_arguments, nullable_rewritten = _fill_nullable_required_defaults(arguments, schema)
        if nullable_rewritten and isinstance(normalized_arguments, dict):
            tool_call = dict(tool_call)
            tool_call["arguments"] = normalized_arguments
            arguments = normalized_arguments
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
    spec, pool = get_bridge_pool(model)
    request_id = uuid.uuid4().hex[:8]
    result = await run_on_bridge_slot(
        pool,
        spec=spec,
        request_id=request_id,
        route="/debug/open-login",
        operation=lambda bridge, _slot_spec: bridge.open_login_page(),
    )
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


@app.post("/v1/responses", response_model=None)
async def responses(request: ResponsesRequest):
    if not is_interface_enabled("openai"):
        raise HTTPException(status_code=404, detail="OpenAI-compatible endpoints are disabled by interface mode.")

    spec, pool = get_bridge_pool(request.model, request.user)
    resolved_model = spec.model_id
    request_id = uuid.uuid4().hex[:8]
    request_tools, request_web_search_tools = split_responses_tools(request.tools)
    bridge_tools = list(request_tools)
    if request_web_search_tools:
        bridge_tools.append(build_provider_web_search_tool(request_web_search_tools))
    request_messages = responses_input_to_chat_messages(
        request.input,
        request.instructions,
        has_tools=bool(bridge_tools),
    )
    if request_web_search_tools:
        request_messages = _append_system_hint(request_messages, PROVIDER_WEB_SEARCH_TOOL_HINT)
    if not request_messages:
        request_messages = [{"role": "user", "content": ""}]
    request_thinking_enabled = resolve_effective_thinking_enabled(
        resolve_responses_thinking_enabled(request),
        spec=spec,
    )
    request_expert_mode_enabled = resolve_effective_expert_mode_enabled(
        resolve_responses_expert_mode_enabled(request),
        spec=spec,
    )
    logger.warning(
        "provider[%s] /v1/responses start model=%s stream=%s messages=%d tools=%d thinking_enabled=%s",
        request_id,
        resolved_model,
        request.stream,
        len(request_messages),
        len(bridge_tools),
        request_thinking_enabled,
    )

    try:
        payload = await run_on_bridge_slot(
            pool,
            spec=spec,
            request_id=request_id,
            route="/v1/responses",
            operation=lambda bridge, slot_spec: bridge_call_with_spec(
                bridge,
                spec=slot_spec,
                messages=request_messages,
                tools=bridge_tools,
                thinking_enabled=request_thinking_enabled,
                expert_mode_enabled=request_expert_mode_enabled,
                output_protocol="openai",
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("provider[%s] /v1/responses bridge.call failed", request_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    raw_text = payload.get("raw_text", "")
    logger.warning(
        "provider[%s] /v1/responses bridge.call done content_chars=%d raw_text_chars=%d tool_calls=%s parse_error=%s retries=%s",
        request_id,
        len(payload.get("content", "") or ""),
        len(raw_text) if isinstance(raw_text, str) else 0,
        summarize_tool_calls(payload.get("tool_calls")),
        payload.get("parse_error"),
        payload.get("protocol_retry_count", 0),
    )

    payload, web_search_items, search_results = await resolve_provider_web_search(
        payload=payload,
        pool=pool,
        spec=spec,
        request_id=request_id,
        request_messages=request_messages,
        bridge_tools=bridge_tools,
        request_thinking_enabled=request_thinking_enabled,
        request_expert_mode_enabled=request_expert_mode_enabled,
        web_search_tools=request_web_search_tools,
    )
    payload = apply_text_tool_call_fallback(payload, request_tools)
    payload = validate_tool_calls_against_schemas(payload, request_tools)
    message, tool_calls, _ = build_openai_assistant_message(payload)
    response_text, response_annotations = append_provider_search_citations(
        message.get("content") or "",
        search_results,
    )
    response_body = build_responses_body(
        model=resolved_model,
        content=response_text,
        tool_calls=tool_calls,
        request=request,
        extra_output_items=web_search_items,
        annotations=response_annotations,
    )
    logger.warning(
        "provider[%s] /v1/responses return ready output_items=%d response_chars=%d",
        request_id,
        len(response_body.get("output", [])),
        len(json.dumps(response_body, ensure_ascii=False)),
    )

    if request.stream:
        return StreamingResponse(
            stream_responses_events(response_body),
            media_type="text/event-stream",
        )

    return response_body


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: ChatCompletionRequest):
    if not is_interface_enabled("openai"):
        raise HTTPException(status_code=404, detail="OpenAI-compatible endpoints are disabled by interface mode.")

    spec, pool = get_bridge_pool(request.model, request.user)
    resolved_model = spec.model_id
    request_id = uuid.uuid4().hex[:8]
    request_messages = [message.model_dump(exclude_none=True) for message in request.messages]
    request_tools = request.tools or []
    request_thinking_enabled = resolve_effective_thinking_enabled(
        resolve_request_thinking_enabled(request),
        spec=spec,
    )
    request_expert_mode_enabled = resolve_effective_expert_mode_enabled(
        resolve_request_expert_mode_enabled(request),
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
        payload = await run_on_bridge_slot(
            pool,
            spec=spec,
            request_id=request_id,
            route="/v1/chat/completions",
            operation=lambda bridge, slot_spec: bridge_call_with_spec(
                bridge,
                spec=slot_spec,
                messages=request_messages,
                tools=request_tools,
                thinking_enabled=request_thinking_enabled,
                expert_mode_enabled=request_expert_mode_enabled,
                output_protocol="openai",
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("provider[%s] /v1 bridge.call failed", request_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    raw_text = payload.get("raw_text", "")
    logger.warning(
        "provider[%s] /v1 bridge.call done content_chars=%d raw_text_chars=%d tool_calls=%s parse_error=%s retries=%s",
        request_id,
        len(payload.get("content", "") or ""),
        len(raw_text) if isinstance(raw_text, str) else 0,
        summarize_tool_calls(payload.get("tool_calls")),
        payload.get("parse_error"),
        payload.get("protocol_retry_count", 0),
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

    spec, pool = get_bridge_pool(request.model, request.user)
    resolved_model = spec.model_id
    request_id = uuid.uuid4().hex[:8]
    bridge_messages = anthropic_messages_to_bridge_payload(request)
    request_tools = anthropic_tools_to_openai_tools(request.tools)
    request_thinking_enabled = resolve_effective_thinking_enabled(
        request.thinking_enabled,
        spec=spec,
    )
    request_expert_mode_enabled = resolve_effective_expert_mode_enabled(
        request.expert_mode_enabled,
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
        payload = await run_on_bridge_slot(
            pool,
            spec=spec,
            request_id=request_id,
            route="/v1/messages",
            operation=lambda bridge, slot_spec: bridge_call_with_spec(
                bridge,
                spec=slot_spec,
                messages=bridge_messages,
                tools=request_tools,
                thinking_enabled=request_thinking_enabled,
                expert_mode_enabled=request_expert_mode_enabled,
                output_protocol="anthropic",
            ),
        )
    except HTTPException:
        raise
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
    spec, pool = get_bridge_pool(request.model)
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
        payload = await run_on_bridge_slot(
            pool,
            spec=spec,
            request_id=request_id,
            route="/debug/chat-timings",
            operation=lambda bridge, slot_spec: bridge_call_with_spec(
                bridge,
                spec=slot_spec,
                messages=request_messages,
                tools=request_tools,
                thinking_enabled=request_thinking_enabled,
                expert_mode_enabled=request_expert_mode_enabled,
                include_debug=True,
                output_protocol="openai",
            ),
        )
    except HTTPException:
        raise
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
    spec, pool = get_bridge_pool(request.model, request.user)
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
        result = await run_on_bridge_slot(
            pool,
            spec=spec,
            request_id=request_id,
            route="/debug/thinking-mode",
            operation=lambda bridge, slot_spec: run_bridge_with_spec(
                bridge,
                spec=slot_spec,
                operation=lambda: bridge.debug_sync_thinking_mode(
                    effective_requested,
                    visible=request.visible,
                ),
            ),
        )
    except HTTPException:
        raise
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


@app.post("/debug/expert-mode")
async def debug_expert_mode(request: ExpertModeRequest) -> dict[str, Any]:
    spec, pool = get_bridge_pool(request.model, request.user)
    resolved_model = spec.model_id
    request_id = uuid.uuid4().hex[:8]
    requested = request.expert_mode_enabled
    effective_requested = resolve_effective_expert_mode_enabled(requested, spec=spec)
    logger.warning(
        "provider[%s] /debug/expert-mode start model=%s requested=%s visible=%s",
        request_id,
        resolved_model,
        effective_requested,
        request.visible,
    )

    try:
        result = await run_on_bridge_slot(
            pool,
            spec=spec,
            request_id=request_id,
            route="/debug/expert-mode",
            operation=lambda bridge, slot_spec: run_bridge_with_spec(
                bridge,
                spec=slot_spec,
                operation=lambda: bridge.debug_sync_expert_mode(
                    effective_requested,
                    visible=request.visible,
                ),
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("provider[%s] /debug/expert-mode failed", request_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    response = {
        "model": resolved_model,
        **result,
    }
    logger.warning(
        "provider[%s] /debug/expert-mode done applied=%s current=%s changed=%s",
        request_id,
        effective_requested,
        result.get("after", {}).get("expert_mode_enabled"),
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
