"""Shared DeepSeek web UI bridge.

This module turns a logged-in DeepSeek web chat page into a local chat backend.
It is intentionally stateless at the API layer by default: each request opens a
fresh page and submits the full conversation transcript, so multiple apps do
not accidentally share a single web-chat context.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from playwright.sync_api import BrowserContext, Error as PlaywrightError
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import Request, WebSocket, sync_playwright

logger = logging.getLogger(__name__)
INVALID_PAYLOAD_DEBUG_PATH = Path("/tmp/deepseek_web_last_invalid_payload.txt")
SALVAGED_PAYLOAD_DEBUG_PATH = Path("/tmp/deepseek_web_last_salvaged_payload.txt")
COPY_CAPTURE_INIT_SCRIPT = """
() => {
  const install = () => {
    window.__deerflowCopyEvents = window.__deerflowCopyEvents || [];
    const clipboard = navigator.clipboard;
    if (!clipboard || typeof clipboard.writeText !== 'function' || clipboard.__deerflowWrapped) {
      return;
    }
    const originalWriteText = clipboard.writeText.bind(clipboard);
    clipboard.writeText = async (text) => {
      window.__deerflowCopyEvents.push({ text });
      try {
        return await originalWriteText(text);
      } catch {
        return undefined;
      }
    };
    clipboard.__deerflowWrapped = true;
  };
  install();
}
"""

DEFAULT_URL = "https://chat.deepseek.com/"
DEFAULT_INPUT_SELECTORS = (
    "textarea",
    '[contenteditable="true"]',
    'textarea[placeholder*="Message"]',
    'textarea[placeholder*="发送"]',
)
DEFAULT_SEND_SELECTORS = (
    'button[type="submit"]',
    'button:has-text("Send")',
    'button:has-text("发送")',
)
DEFAULT_NEW_CHAT_SELECTORS = (
    'a[href="/new"]',
    'button:has-text("New Chat")',
    'button:has-text("New chat")',
    'button:has-text("新对话")',
    'button:has-text("新建对话")',
    'button:has-text("开启新对话")',
    '[aria-label*="New Chat"]',
    '[aria-label*="New chat"]',
    '[aria-label*="新对话"]',
)
DEFAULT_ASSISTANT_SELECTORS = (
    '[data-message-author-role="assistant"]',
    '[data-role="assistant"]',
    ".ds-markdown",
    ".markdown",
    '[class*="message"]',
)
PROMPT_REPLAY_MARKERS = (
    "You are acting as the backend LLM for a local OpenAI-compatible gateway.",
    "Continue the existing DeerFlow session already initialized in this chat.",
    "Return exactly one JSON object with this schema:",
)
SCHEMA_EXAMPLE_MARKERS = (
    '"content":"string"',
    '"name":"string"',
    '"arguments":{}',
    '"id":"string"',
)
CHAT_URL_RE = re.compile(r"^https://chat\.deepseek\.com/a/chat/s/[A-Za-z0-9-]+/?$")


@dataclass
class DeepSeekTrace:
    started_at: float = field(default_factory=time.perf_counter)
    marks: dict[str, float] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)

    def mark(self, name: str) -> None:
        self.marks[name] = time.perf_counter()

    def set(self, name: str, value: Any) -> None:
        self.values[name] = value

    def as_dict(self) -> dict[str, Any]:
        points = {"start": self.started_at, **self.marks}
        ordered = list(points.items())
        steps: dict[str, int] = {}
        previous_name, previous_time = ordered[0]
        for name, timestamp in ordered[1:]:
            steps[f"{previous_name}->{name}_ms"] = int((timestamp - previous_time) * 1000)
            previous_name, previous_time = name, timestamp

        total_ms = int((previous_time - self.started_at) * 1000)
        return {
            "steps_ms": steps,
            "total_ms": total_ms,
            **self.values,
        }


@dataclass
class TransportTextCandidate:
    source: str
    url: str
    text: str
    captured_at: float = field(default_factory=time.perf_counter)


def normalize_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [normalize_text_content(item) for item in content]
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        for key in ("text", "content", "output"):
            value = content.get(key)
            if isinstance(value, str):
                return value
            if value is not None:
                nested = normalize_text_content(value)
                if nested:
                    return nested
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def repair_jsonish_text(text: str) -> str:
    repaired: list[str] = []
    in_string = False
    escaped = False
    i = 0
    length = len(text)

    while i < length:
        char = text[i]

        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
            i += 1
            continue

        if escaped:
            repaired.append(char)
            escaped = False
            i += 1
            continue

        if char == "\\":
            next_char = text[i + 1] if i + 1 < length else ""
            if next_char in {'"', "\\", "/", "b", "f", "n", "r", "t"}:
                repaired.append(char)
                escaped = True
            elif next_char == "u" and i + 5 < length:
                repaired.append(char)
                escaped = True
            else:
                repaired.append("\\\\")
            i += 1
            continue

        if char == '"':
            j = i + 1
            while j < length and text[j] in " \t\r\n":
                j += 1
            next_non_ws = text[j] if j < length else ""
            if next_non_ws in {",", "}", "]", ":", ""}:
                repaired.append(char)
                in_string = False
            else:
                repaired.append('\\"')
            i += 1
            continue

        if char == "\n":
            repaired.append("\\n")
            i += 1
            continue

        if char == "\r":
            repaired.append("\\r")
            i += 1
            continue

        repaired.append(char)
        i += 1

    return "".join(repaired)


def balance_jsonish_object(text: str) -> str:
    balance = 0
    in_string = False
    escaped = False

    for char in text:
        if escaped:
            escaped = False
            continue

        if in_string:
            if char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            balance += 1
        elif char == "}":
            balance = max(0, balance - 1)

    if balance <= 0:
        return text
    return text + ("}" * balance)


def load_jsonish_object(text: str) -> dict[str, Any] | None:
    repaired = repair_jsonish_text(text)
    candidates = [repaired]

    balanced = balance_jsonish_object(repaired)
    if balanced != repaired:
        candidates.append(balanced)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed

    relaxed = parse_relaxed_jsonish(text)
    if isinstance(relaxed, dict):
        return relaxed

    return None


def _decode_json_string_literal(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return value.replace('\\"', '"').replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t").replace("\\\\", "\\")


def _extract_json_string_value(text: str, marker: str) -> str | None:
    marker_start = text.find(marker)
    if marker_start < 0:
        return None

    i = marker_start + len(marker)
    chars: list[str] = []
    escaped = False

    while i < len(text):
        char = text[i]
        if escaped:
            chars.append("\\" + char)
            escaped = False
            i += 1
            continue
        if char == "\\":
            escaped = True
            i += 1
            continue
        if char == '"':
            return _decode_json_string_literal("".join(chars))
        chars.append(char)
        i += 1

    return None


def _extract_json_string_value_relaxed(text: str, marker: str, *, prefer_longest: bool = False) -> str | None:
    marker_start = text.find(marker)
    if marker_start < 0:
        return None

    value_start = marker_start + len(marker)
    synthetic = '"' + text[value_start:]
    candidates = iter_relaxed_string_value_candidates(synthetic, 0)
    if not candidates:
        return None

    if prefer_longest:
        return max(candidates, key=lambda item: len(item[0]))[0]
    return candidates[0][0]


def _extract_last_json_string_value_before_object_end(text: str, marker: str) -> str | None:
    marker_start = text.find(marker)
    if marker_start < 0:
        return None

    candidate = text.rstrip()
    if not candidate.endswith("}"):
        return None

    value_start = marker_start + len(marker)
    end_quote = candidate.rfind('"')
    if end_quote < value_start:
        return None

    return _decode_json_string_literal(candidate[value_start:end_quote])


def _extract_balanced_jsonish_block(text: str, start: int) -> tuple[str, int] | None:
    if start < 0 or start >= len(text) or text[start] not in "{[":
        return None

    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue

        if char == opening:
            depth += 1
            continue

        if char == closing:
            depth -= 1
            if depth == 0:
                return text[start : index + 1], index + 1

    return None


def _find_jsonish_token_outside_strings(text: str, token: str, start: int = 0) -> int:
    if not token:
        return -1

    in_string = False
    escaped = False
    index = max(0, start)

    while index < len(text):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if text.startswith(token, index):
            return index

        if char == '"':
            in_string = True
            index += 1
            continue

        index += 1

    return -1


def salvage_known_tool_arguments(name: str, arguments_blob: str, parsed_arguments: dict[str, Any] | None = None) -> dict[str, Any] | None:
    merged = dict(parsed_arguments or {})

    for key in ("description", "path"):
        value = _extract_json_string_value_relaxed(arguments_blob, f'"{key}":"')
        if value is not None:
            merged[key] = value

    if name == "write_file":
        content = _extract_last_json_string_value_before_object_end(arguments_blob, '"content":"')
        if content is None:
            content = _extract_json_string_value_relaxed(arguments_blob, '"content":"', prefer_longest=True)
        if content is not None and len(content) >= len(str(merged.get("content", ""))):
            merged["content"] = content
    elif name == "bash":
        command = _extract_last_json_string_value_before_object_end(arguments_blob, '"command":"')
        if command is None:
            command = _extract_json_string_value_relaxed(arguments_blob, '"command":"', prefer_longest=True)
        if command is not None and len(command) >= len(str(merged.get("command", ""))):
            merged["command"] = command

    return merged or None


def salvage_tool_calls_payload(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    if not candidate or '"tool_calls"' not in candidate:
        return None

    content = _extract_json_string_value(candidate, '"content":"') or ""

    normalized_tool_calls: list[dict[str, Any]] = []
    search_start = 0
    while True:
        name_key = '"name":"'
        name_start = candidate.find(name_key, search_start)
        if name_start < 0:
            break
        name_value_start = name_start + len(name_key)
        name_end = candidate.find('"', name_value_start)
        if name_end < 0:
            break
        name = candidate[name_value_start:name_end]

        arguments_key = '"arguments":'
        arguments_start = candidate.find(arguments_key, name_end)
        if arguments_start < 0:
            break
        arguments_value_start = candidate.find("{", arguments_start + len(arguments_key))
        if arguments_value_start < 0:
            break

        call_id = None
        id_key = '"id":"'
        id_start = _find_jsonish_token_outside_strings(candidate, id_key, arguments_value_start)
        next_tool_start = _find_jsonish_token_outside_strings(candidate, '{"name":"', arguments_value_start)
        next_array_end = _find_jsonish_token_outside_strings(candidate, "]", arguments_value_start)
        raw_next_tool_start = candidate.find('{"name":"', arguments_value_start + 1)
        raw_search_end = len(candidate)
        if raw_next_tool_start >= 0:
            raw_search_end = min(raw_search_end, raw_next_tool_start)
        raw_last_id_start = candidate.rfind(id_key, arguments_value_start, raw_search_end)
        if raw_last_id_start > id_start:
            id_start = raw_last_id_start
        id_within_bounds = id_start >= 0
        if next_tool_start >= 0 and id_start > next_tool_start:
            id_within_bounds = False
        if next_array_end >= 0 and id_start > next_array_end:
            id_within_bounds = False

        if id_within_bounds:
            arguments_blob = candidate[arguments_value_start:id_start].rstrip(", \n\r\t")
            search_after_arguments = id_start
            id_value_start = id_start + len(id_key)
            id_end = candidate.find('"', id_value_start)
            if id_end >= 0:
                call_id = candidate[id_value_start:id_end]
                search_after_arguments = id_end
        else:
            arguments_block = _extract_balanced_jsonish_block(candidate, arguments_value_start)
            if arguments_block is None:
                break
            arguments_blob, search_after_arguments = arguments_block

        parsed_arguments = load_jsonish_object(arguments_blob)
        arguments = salvage_known_tool_arguments(name, arguments_blob, parsed_arguments)
        if arguments is None:
            search_start = search_after_arguments
            continue

        normalized_tool_calls.append(
            {
                "id": call_id or f"call_{uuid.uuid4().hex[:12]}",
                "name": name,
                "arguments": arguments,
            }
        )
        search_start = search_after_arguments

    if not normalized_tool_calls:
        return None

    return {
        "content": content,
        "tool_calls": normalized_tool_calls,
        "raw_text": text,
    }


def looks_like_tool_payload(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.startswith("{") and '"tool_calls"' in stripped:
        return True
    return salvage_tool_calls_payload(stripped) is not None


def looks_like_assistant_payload_candidate(text: str) -> bool:
    stripped = text.strip()
    if not stripped or not stripped.startswith("{"):
        return False
    if looks_like_tool_payload(stripped):
        return True
    return '"content"' in stripped or '"tool_' in stripped or '"tool_calls"' in stripped


def choose_best_assistant_text(candidates: list[str]) -> str:
    normalized = [candidate.strip() for candidate in candidates if isinstance(candidate, str) and candidate.strip()]
    if not normalized:
        return ""

    unique_candidates = list(dict.fromkeys(normalized))
    json_like = [candidate for candidate in unique_candidates if looks_like_assistant_payload_candidate(candidate)]
    if json_like:
        return max(json_like, key=len)

    return max(unique_candidates, key=len)


def assistant_candidate_score(candidate: dict[str, Any]) -> tuple[int, int, int]:
    text = candidate.get("text")
    if not isinstance(text, str):
        return (-10_000, 0, -1)

    stripped = text.strip()
    if not stripped:
        return (-10_000, 0, -1)

    score = 0
    if looks_like_assistant_payload_candidate(stripped):
        score += 100
    if is_assistant_payload_text(stripped):
        score += 50
    if any(marker in stripped for marker in PROMPT_REPLAY_MARKERS):
        score -= 250

    index = candidate.get("index")
    try:
        normalized_index = int(index)
    except Exception:
        normalized_index = -1

    return (score, len(stripped), normalized_index)


def choose_best_assistant_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    normalized: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        text = candidate.get("text")
        if not isinstance(text, str):
            continue
        stripped = text.strip()
        if not stripped:
            continue
        index = candidate.get("index")
        try:
            normalized_index = int(index)
        except Exception:
            normalized_index = -1
        normalized.append(
            {
                "index": normalized_index,
                "text": stripped,
            }
        )

    if not normalized:
        return None

    return max(normalized, key=assistant_candidate_score)


def is_schema_example_payload_text(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and all(marker in stripped for marker in SCHEMA_EXAMPLE_MARKERS)


def payload_candidate_score(candidate: dict[str, Any]) -> tuple[int, int, int]:
    text = candidate.get("text")
    if not isinstance(text, str):
        return (-10_000, 0, -1)

    stripped = text.strip()
    if not stripped:
        return (-10_000, 0, -1)
    if is_schema_example_payload_text(stripped):
        return (-9_000, len(stripped), -1)

    score = 0
    if looks_like_assistant_payload_candidate(stripped):
        score += 100
    if is_assistant_payload_text(stripped):
        score += 200

    dom_index = candidate.get("domIndex")
    try:
        normalized_index = int(dom_index)
    except Exception:
        normalized_index = -1

    return (score, len(stripped), normalized_index)


def choose_best_payload_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    normalized: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        text = candidate.get("text")
        if not isinstance(text, str):
            continue
        stripped = text.strip()
        if not stripped:
            continue
        if is_schema_example_payload_text(stripped):
            continue
        dom_index = candidate.get("domIndex")
        probe_id = candidate.get("probeId")
        try:
            normalized_index = int(dom_index)
        except Exception:
            normalized_index = -1
        normalized.append(
            {
                "probeId": probe_id,
                "domIndex": normalized_index,
                "text": stripped,
            }
        )

    if not normalized:
        return None

    return max(normalized, key=payload_candidate_score)


def extract_payload_text_candidates(text: str) -> list[str]:
    if not text:
        return []

    candidates: list[str] = []
    search_start = 0
    while True:
        payload_start = text.find('{"content"', search_start)
        if payload_start < 0:
            break
        payload_block = _extract_balanced_jsonish_block(text, payload_start)
        if payload_block is None:
            candidates.append(text[payload_start:].strip())
            break
        candidates.append(payload_block[0].strip())
        search_start = payload_start + 1

    stripped = text.strip()
    if stripped.startswith("{") and '"tool_calls"' in stripped:
        candidates.append(stripped)

    return list(dict.fromkeys(candidate for candidate in candidates if candidate and '"tool_calls"' in candidate))


def is_assistant_payload_dict(payload: Any) -> bool:
    return isinstance(payload, dict) and "content" in payload and "tool_calls" in payload


def is_assistant_payload_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if is_schema_example_payload_text(stripped):
        return False

    try:
        payload = extract_json_object(stripped)
    except Exception:
        payload = None

    if is_assistant_payload_dict(payload):
        return True

    return salvage_tool_calls_payload(stripped) is not None


def _append_transport_payload_candidates(candidates: list[str], value: Any) -> None:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return
        if is_assistant_payload_text(stripped):
            candidates.append(stripped)
        return

    if isinstance(value, dict):
        if is_assistant_payload_dict(value):
            candidates.append(
                json.dumps(
                    {
                        "content": value.get("content", ""),
                        "tool_calls": value.get("tool_calls", []),
                    },
                    ensure_ascii=False,
                )
            )
        for nested in value.values():
            _append_transport_payload_candidates(candidates, nested)
        return

    if isinstance(value, list):
        for nested in value:
            _append_transport_payload_candidates(candidates, nested)


def extract_transport_payload_candidates(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []

    candidates: list[str] = []
    _append_transport_payload_candidates(candidates, stripped)

    try:
        parsed = json.loads(stripped)
    except Exception:
        parsed = None
    if parsed is not None:
        _append_transport_payload_candidates(candidates, parsed)

    if "data:" in stripped:
        for line in stripped.splitlines():
            candidate = line.strip()
            if not candidate.startswith("data:"):
                continue
            payload = candidate[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            _append_transport_payload_candidates(candidates, payload)
            try:
                parsed_line = json.loads(payload)
            except Exception:
                continue
            _append_transport_payload_candidates(candidates, parsed_line)

    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def payload_text_score(text: str) -> tuple[int, int]:
    stripped = text.strip()
    if not stripped:
        return (0, 0)

    if is_assistant_payload_text(stripped):
        return (4, len(stripped))

    return (0, len(stripped))


def choose_best_payload_text(candidates: list[str]) -> str:
    normalized = [candidate.strip() for candidate in candidates if isinstance(candidate, str) and candidate.strip()]
    if not normalized:
        return ""
    unique_candidates = list(dict.fromkeys(normalized))
    return max(unique_candidates, key=payload_text_score)


def skip_jsonish_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def decode_relaxed_string_fragment(fragment: str) -> str:
    chars: list[str] = []
    i = 0
    while i < len(fragment):
        char = fragment[i]
        if char != "\\":
            chars.append(char)
            i += 1
            continue

        i += 1
        if i >= len(fragment):
            chars.append("\\")
            break

        escaped = fragment[i]
        if escaped == "n":
            chars.append("\n")
        elif escaped == "r":
            chars.append("\r")
        elif escaped == "t":
            chars.append("\t")
        elif escaped == "b":
            chars.append("\b")
        elif escaped == "f":
            chars.append("\f")
        elif escaped in {'"', "\\", "/"}:
            chars.append(escaped)
        elif escaped == "u" and i + 4 < len(fragment):
            hex_code = fragment[i + 1 : i + 5]
            try:
                chars.append(chr(int(hex_code, 16)))
                i += 4
            except Exception:
                chars.append("\\u" + hex_code)
                i += 4
        else:
            chars.append(escaped)
        i += 1

    return "".join(chars)


def parse_strict_json_string(text: str, index: int) -> tuple[str, int] | None:
    if index >= len(text) or text[index] != '"':
        return None

    chars: list[str] = []
    escaped = False
    i = index + 1
    while i < len(text):
        char = text[i]
        if escaped:
            chars.append("\\" + char)
            escaped = False
            i += 1
            continue
        if char == "\\":
            escaped = True
            i += 1
            continue
        if char == '"':
            return decode_relaxed_string_fragment("".join(chars)), i + 1
        chars.append(char)
        i += 1

    return None


def iter_relaxed_string_value_candidates(text: str, index: int) -> list[tuple[str, int]]:
    if index >= len(text) or text[index] != '"':
        return []

    candidates: list[tuple[str, int]] = []
    escaped = False
    i = index + 1
    while i < len(text):
        char = text[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if char == "\\":
            escaped = True
            i += 1
            continue
        if char == '"':
            separator_index = skip_jsonish_ws(text, i + 1)
            if separator_index >= len(text):
                candidates.append((decode_relaxed_string_fragment(text[index + 1 : i]), i + 1))
            elif text[separator_index] == ",":
                next_index = skip_jsonish_ws(text, separator_index + 1)
                if next_index < len(text) and text[next_index] == '"':
                    candidates.append((decode_relaxed_string_fragment(text[index + 1 : i]), i + 1))
            elif text[separator_index] in {"}", "]"}:
                candidates.append((decode_relaxed_string_fragment(text[index + 1 : i]), i + 1))
        i += 1

    return candidates


def parse_relaxed_jsonish_value(text: str, index: int) -> tuple[Any, int] | None:
    index = skip_jsonish_ws(text, index)
    if index >= len(text):
        return None

    char = text[index]
    if char == "{":
        return parse_relaxed_jsonish_object(text, index)
    if char == "[":
        return parse_relaxed_jsonish_array(text, index)
    if char == '"':
        candidates = iter_relaxed_string_value_candidates(text, index)
        return candidates[0] if candidates else None

    end = index
    while end < len(text) and text[end] not in ",}]":
        end += 1
    token = text[index:end].strip()
    if not token:
        return None
    try:
        return json.loads(token), end
    except Exception:
        return token, end


def parse_relaxed_jsonish_object(text: str, index: int = 0) -> tuple[dict[str, Any], int] | None:
    if index >= len(text) or text[index] != "{":
        return None

    def parse_members(position: int, current: dict[str, Any]) -> tuple[dict[str, Any], int] | None:
        position = skip_jsonish_ws(text, position)
        if position >= len(text):
            return None
        if text[position] == "}":
            return dict(current), position + 1

        key_result = parse_strict_json_string(text, position)
        if key_result is None:
            return None
        key, position = key_result
        position = skip_jsonish_ws(text, position)
        if position >= len(text) or text[position] != ":":
            return None
        position = skip_jsonish_ws(text, position + 1)
        if position >= len(text):
            return None

        if text[position] == '"':
            for value, next_position in iter_relaxed_string_value_candidates(text, position):
                separator_index = skip_jsonish_ws(text, next_position)
                current[key] = value
                if separator_index < len(text) and text[separator_index] == ",":
                    result = parse_members(separator_index + 1, current)
                    if result is not None:
                        return result
                elif separator_index < len(text) and text[separator_index] == "}":
                    return dict(current), separator_index + 1
                current.pop(key, None)
            return None

        value_result = parse_relaxed_jsonish_value(text, position)
        if value_result is None:
            return None
        value, next_position = value_result
        separator_index = skip_jsonish_ws(text, next_position)
        current[key] = value
        if separator_index < len(text) and text[separator_index] == ",":
            result = parse_members(separator_index + 1, current)
            if result is not None:
                return result
        elif separator_index < len(text) and text[separator_index] == "}":
            return dict(current), separator_index + 1
        current.pop(key, None)
        return None

    return parse_members(index + 1, {})


def parse_relaxed_jsonish_array(text: str, index: int = 0) -> tuple[list[Any], int] | None:
    if index >= len(text) or text[index] != "[":
        return None

    def parse_items(position: int, current: list[Any]) -> tuple[list[Any], int] | None:
        position = skip_jsonish_ws(text, position)
        if position >= len(text):
            return None
        if text[position] == "]":
            return list(current), position + 1

        value_result = parse_relaxed_jsonish_value(text, position)
        if value_result is None:
            return None
        value, next_position = value_result
        separator_index = skip_jsonish_ws(text, next_position)
        current.append(value)
        if separator_index < len(text) and text[separator_index] == ",":
            result = parse_items(separator_index + 1, current)
            if result is not None:
                return result
        elif separator_index < len(text) and text[separator_index] == "]":
            return list(current), separator_index + 1
        current.pop()
        return None

    return parse_items(index + 1, [])


def parse_relaxed_jsonish(text: str) -> Any | None:
    candidate = text.strip()
    if not candidate:
        return None

    value_result = parse_relaxed_jsonish_value(candidate, 0)
    if value_result is None:
        return None

    value, next_index = value_result
    next_index = skip_jsonish_ws(candidate, next_index)
    if next_index != len(candidate):
        return None
    return value


def extract_json_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if not candidate:
        raise ValueError("Empty response")

    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3:
            candidate = "\n".join(lines[1:-1]).strip()

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        repaired = repair_jsonish_text(candidate)
        try:
            parsed = json.loads(repaired)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        object_text = candidate[start : end + 1]
        try:
            parsed = json.loads(object_text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            repaired = repair_jsonish_text(object_text)
            parsed = json.loads(repaired)
            if isinstance(parsed, dict):
                return parsed

    relaxed = parse_relaxed_jsonish(candidate)
    if isinstance(relaxed, dict):
        return relaxed

    raise ValueError(f"Could not parse JSON object from response: {text[:500]}")


class DeepSeekTransportCapture:
    def __init__(self, page: Page) -> None:
        self.page = page
        self._installed = False
        self._candidates: list[TransportTextCandidate] = []
        self._cdp_session: Any = None
        self._cdp_handlers: list[tuple[str, Any]] = []
        self._cdp_websocket_urls: dict[str, str] = {}
        self._websocket_handlers: list[tuple[WebSocket, Any]] = []

    @property
    def candidate_count(self) -> int:
        return len(self._candidates)

    def install(self) -> None:
        if self._installed:
            return
        try:
            self._cdp_session = self.page.context.new_cdp_session(self.page)
            self._cdp_session.send("Network.enable")
            self._cdp_session.on("Network.webSocketCreated", self._on_cdp_websocket_created)
            self._cdp_session.on("Network.webSocketFrameReceived", self._on_cdp_websocket_frame_received)
            self._cdp_handlers.extend(
                [
                    ("Network.webSocketCreated", self._on_cdp_websocket_created),
                    ("Network.webSocketFrameReceived", self._on_cdp_websocket_frame_received),
                ]
            )
        except Exception:
            self._cdp_session = None

        self.page.on("requestfinished", self._on_requestfinished)
        self.page.on("websocket", self._on_websocket)
        self._installed = True

    def close(self) -> None:
        if not self._installed:
            return
        try:
            self.page.remove_listener("requestfinished", self._on_requestfinished)
        except Exception:
            pass
        try:
            self.page.remove_listener("websocket", self._on_websocket)
        except Exception:
            pass
        if self._cdp_session is not None:
            for event_name, handler in self._cdp_handlers:
                try:
                    self._cdp_session.remove_listener(event_name, handler)
                except Exception:
                    pass
            try:
                self._cdp_session.detach()
            except Exception:
                pass
        self._cdp_session = None
        self._cdp_handlers.clear()
        self._cdp_websocket_urls.clear()
        for websocket, handler in self._websocket_handlers:
            try:
                websocket.remove_listener("framereceived", handler)
            except Exception:
                pass
        self._websocket_handlers.clear()
        self._installed = False

    def best_payload_text(self) -> str:
        payload_candidates = [candidate.text for candidate in self._candidates]
        return choose_best_payload_text(payload_candidates)

    def _record_text(self, *, source: str, url: str, text: str) -> None:
        for candidate_text in extract_transport_payload_candidates(text):
            self._candidates.append(
                TransportTextCandidate(
                    source=source,
                    url=url,
                    text=candidate_text,
                )
            )

    def _on_requestfinished(self, request: Request) -> None:
        try:
            response = request.response()
        except Exception:
            return
        if response is None:
            return

        resource_type = request.resource_type or ""
        if resource_type not in {"fetch", "xhr", "eventsource", "other"}:
            return

        try:
            content_type = (response.headers.get("content-type") or "").lower()
        except Exception:
            content_type = ""

        if not any(token in content_type for token in ("json", "text", "event-stream", "javascript")):
            url_hint = request.url.lower()
            if not any(token in url_hint for token in ("chat", "completion", "conversation", "message", "assistant")):
                return

        try:
            text = response.text()
        except Exception:
            return

        self._record_text(source=f"http:{resource_type}", url=request.url, text=text)

    def _on_websocket(self, websocket: WebSocket) -> None:
        def handle_frame(frame: bytes | str, *, ws: WebSocket = websocket) -> None:
            if isinstance(frame, bytes):
                try:
                    text = frame.decode("utf-8")
                except Exception:
                    return
            else:
                text = frame
            self._record_text(source="websocket", url=ws.url, text=text)

        websocket.on("framereceived", handle_frame)
        self._websocket_handlers.append((websocket, handle_frame))

    def _on_cdp_websocket_created(self, payload: dict[str, Any]) -> None:
        request_id = payload.get("requestId")
        url = payload.get("url")
        if isinstance(request_id, str) and isinstance(url, str):
            self._cdp_websocket_urls[request_id] = url

    def _on_cdp_websocket_frame_received(self, payload: dict[str, Any]) -> None:
        request_id = payload.get("requestId")
        response = payload.get("response") or {}
        data = response.get("payloadData")
        if not isinstance(data, str):
            return
        url = self._cdp_websocket_urls.get(request_id, "")
        self._record_text(source="cdp:websocket", url=url, text=data)


class DeepSeekWebBridge:
    """Bridge a logged-in DeepSeek browser session into a local callable backend."""

    def __init__(
        self,
        *,
        url: str = DEFAULT_URL,
        user_data_dir: str = "~/.deerflow/deepseek-web-profile",
        headless: bool = False,
        page_load_timeout_ms: int = 30_000,
        response_timeout_ms: int = 180_000,
        stable_poll_interval_ms: int = 1200,
        stable_rounds: int = 3,
        input_selectors: tuple[str, ...] = DEFAULT_INPUT_SELECTORS,
        send_selectors: tuple[str, ...] = DEFAULT_SEND_SELECTORS,
        new_chat_selectors: tuple[str, ...] = DEFAULT_NEW_CHAT_SELECTORS,
        assistant_selectors: tuple[str, ...] = DEFAULT_ASSISTANT_SELECTORS,
        force_new_chat: bool = True,
        sticky_marker: str | None = None,
        sticky_scan_chars: int = 8000,
        sticky_reanchor_messages: int | None = 24,
        session_state_path: str | None = None,
        reuse_persisted_chat: bool = False,
    ) -> None:
        self.url = url
        self.user_data_dir = user_data_dir
        self.headless = headless
        self.page_load_timeout_ms = page_load_timeout_ms
        self.response_timeout_ms = response_timeout_ms
        self.stable_poll_interval_ms = stable_poll_interval_ms
        self.stable_rounds = stable_rounds
        self.input_selectors = input_selectors
        self.send_selectors = send_selectors
        self.new_chat_selectors = new_chat_selectors
        self.assistant_selectors = assistant_selectors
        self.force_new_chat = force_new_chat
        self.sticky_marker = sticky_marker
        self.sticky_scan_chars = sticky_scan_chars
        self.sticky_reanchor_messages = sticky_reanchor_messages
        self.session_state_path = session_state_path
        self.reuse_persisted_chat = reuse_persisted_chat

        self._playwright = None
        self._browser_type = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._actual_headless: bool | None = None
        self._sticky_initialized = False
        self._sticky_last_messages: list[dict[str, Any]] = []
        self._sticky_messages_since_full = 0
        self._persisted_chat_url: str | None = None
        self._state_loaded = False
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            if self._context is not None:
                self._context.close()
            if self._playwright is not None:
                self._playwright.stop()
            self._context = None
            self._page = None
            self._actual_headless = None
            self._browser_type = None
            self._playwright = None
            self._sticky_initialized = False
            self._sticky_last_messages = []
            self._sticky_messages_since_full = 0
            self._persisted_chat_url = None
            self._state_loaded = False

    def _load_session_state(self) -> None:
        if self._state_loaded:
            return
        self._state_loaded = True
        if not self.session_state_path:
            return
        path = Path(self.session_state_path).expanduser().resolve()
        try:
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("Failed to load DeepSeek session state from %s.", path, exc_info=True)
            return

        chat_url = data.get("chat_url")
        if isinstance(chat_url, str) and CHAT_URL_RE.match(chat_url):
            self._persisted_chat_url = chat_url.rstrip("/")

        last_messages = data.get("sticky_last_messages")
        if isinstance(last_messages, list):
            self._sticky_last_messages = [
                item for item in last_messages if isinstance(item, dict)
            ]

        messages_since_full = data.get("sticky_messages_since_full")
        if isinstance(messages_since_full, int) and messages_since_full >= 0:
            self._sticky_messages_since_full = messages_since_full

        sticky_initialized = data.get("sticky_initialized")
        if isinstance(sticky_initialized, bool):
            self._sticky_initialized = sticky_initialized

    def _save_session_state(self) -> None:
        if not self.session_state_path:
            return
        path = Path(self.session_state_path).expanduser().resolve()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "chat_url": self._persisted_chat_url,
                        "sticky_initialized": self._sticky_initialized,
                        "sticky_last_messages": self._sticky_last_messages,
                        "sticky_messages_since_full": self._sticky_messages_since_full,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("Failed to save DeepSeek session state to %s.", path, exc_info=True)

    def _refresh_current_chat_url(self, page: Page, *, persist: bool = True) -> None:
        current_url = (page.url or "").rstrip("/")
        if not CHAT_URL_RE.match(current_url):
            return
        self._persisted_chat_url = current_url
        if persist:
            self._save_session_state()

    def ensure_context(self, *, headless: bool | None = None) -> BrowserContext:
        with self._lock:
            self._load_session_state()
            requested_headless = self.headless if headless is None else headless
            if self._context is not None and self._actual_headless == requested_headless:
                return self._context

            if self._context is not None:
                self.close()

            profile_dir = str(Path(self.user_data_dir).expanduser().resolve())
            Path(profile_dir).mkdir(parents=True, exist_ok=True)

            self._playwright = sync_playwright().start()
            self._browser_type = self._playwright.chromium
            self._context = self._browser_type.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=requested_headless,
                viewport={"width": 1440, "height": 960},
            )
            self._actual_headless = requested_headless
            return self._context

    def ensure_page(self, *, visible: bool = False) -> Page:
        context = self.ensure_context(headless=False if visible else self.headless)
        if self._page is not None and not self._page.is_closed():
            return self._page

        self._page = context.new_page()
        self._page.add_init_script(COPY_CAPTURE_INIT_SCRIPT)
        try:
            self._page.evaluate(COPY_CAPTURE_INIT_SCRIPT)
        except Exception:
            logger.debug("Failed to install clipboard capture on DeepSeek page.", exc_info=True)
        return self._page

    def reset_page(self) -> None:
        if self._page is None or self._page.is_closed():
            self._page = None
            return
        try:
            self._page.close()
        except Exception:
            logger.debug("Failed to close DeepSeek page before recreating it.", exc_info=True)
        finally:
            self._page = None

    def open_login_page(self) -> dict[str, Any]:
        """Open a persistent DeepSeek page so the user can log in manually."""
        with self._lock:
            page = self.ensure_page(visible=True)
            page.goto(self.url, wait_until="domcontentloaded", timeout=self.page_load_timeout_ms)
            return {
                "url": page.url,
                "profile_dir": str(Path(self.user_data_dir).expanduser().resolve()),
                "headless": False,
            }

    def call(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        include_debug: bool = False,
    ) -> dict[str, Any]:
        trace = DeepSeekTrace()
        raw_text = self.submit_prompt(messages=messages, tools=tools or [], trace=trace)
        payload = self.parse_model_payload(raw_text)
        timing = trace.as_dict()
        logger.info("DeepSeek web bridge timing: %s", timing)
        if include_debug:
            payload["debug"] = {"timing": timing}
        return payload

    def ensure_chat_ready(self, page: Page, *, trace: DeepSeekTrace | None = None) -> None:
        preferred_url = self._persisted_chat_url if self.reuse_persisted_chat else None
        if preferred_url:
            current_url = (page.url or "").rstrip("/")
            if current_url != preferred_url:
                try:
                    page.goto(preferred_url, wait_until="domcontentloaded", timeout=self.page_load_timeout_ms)
                    self._refresh_current_chat_url(page, persist=False)
                    self.first_visible(page, self.input_selectors)
                    if trace is not None:
                        trace.set("page_reused", current_url == preferred_url)
                        trace.set("preferred_chat_url", preferred_url)
                        trace.mark("page_ready")
                    return
                except Exception:
                    logger.debug("Failed to reuse persisted DeepSeek chat URL %s.", preferred_url, exc_info=True)

        current_url = page.url or ""
        if current_url.startswith(self.url.rstrip("/")):
            try:
                self.first_visible(page, self.input_selectors)
                self._refresh_current_chat_url(page, persist=False)
                if trace is not None:
                    trace.set("page_reused", True)
                    trace.mark("page_ready")
                return
            except Exception:
                pass

        page.goto(self.url, wait_until="domcontentloaded", timeout=self.page_load_timeout_ms)
        self._refresh_current_chat_url(page, persist=False)
        if trace is not None:
            trace.set("page_reused", False)
            trace.mark("page_ready")

    def build_full_prompt(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> str:
        parts = [
            "You are acting as the backend LLM for a local OpenAI-compatible gateway.",
            "Return exactly one JSON object with this schema:",
            '{"content":"string","tool_calls":[{"name":"string","arguments":{},"id":"string"}]}',
            "Rules:",
            "- Return JSON only.",
            '- If no tool call is needed, set "tool_calls" to [].',
            '- If a tool call is needed, content may be empty.',
            "- Do not use markdown fences.",
            "- If the user asks to create, edit, read, list, search, or run something and matching tools are available, you MUST use tool_calls instead of only describing what would happen.",
            "- Never claim a file was written, modified, or listed unless you emitted the corresponding tool call.",
            '- Use a normal "arguments" object for tool calls.',
            '- Every string inside "arguments" must be valid JSON string content.',
            '- Escape embedded double quotes as \\\", backslashes as \\\\, and newlines as \\n.',
            '- Example: {"name":"write_file","arguments":{"path":"/tmp/a.py","content":"print(\\\"hi\\\")\\n"}}',
        ]
        if self.sticky_marker:
            parts.append(f"Session marker: {self.sticky_marker}")
        if tools:
            parts.append("Available tools:")
            parts.append(json.dumps(tools, ensure_ascii=False, indent=2))
        else:
            parts.append("No tools are available for this request.")

        parts.append("Conversation:")
        for message in messages:
            role = message.get("role", "user").upper()
            parts.append(f"[{role}]\n{json.dumps(message, ensure_ascii=False, indent=2)}")

        return "\n\n".join(parts)

    def build_delta_prompt(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> str:
        parts = [
            "Continue the existing DeerFlow session already initialized in this chat.",
            "Return exactly one JSON object with this schema:",
            '{"content":"string","tool_calls":[{"name":"string","arguments":{},"id":"string"}]}',
            "Rules:",
            "- Return JSON only.",
            '- If no tool call is needed, set "tool_calls" to [].',
            '- If a tool call is needed, content may be empty.',
            "- Do not use markdown fences.",
            "- Follow the previously established DeerFlow system instructions already present in this conversation.",
            '- Use a normal "arguments" object for tool calls.',
            '- Every string inside "arguments" must be valid JSON string content.',
            '- Escape embedded double quotes as \\\", backslashes as \\\\, and newlines as \\n.',
            '- Example: {"name":"write_file","arguments":{"path":"/tmp/a.py","content":"print(\\\"hi\\\")\\n"}}',
        ]
        if self.sticky_marker:
            parts.append(f"Confirmed session marker: {self.sticky_marker}")
        if tools:
            parts.append("Available tools for this turn:")
            parts.append(json.dumps(tools, ensure_ascii=False, indent=2))
        parts.append("New conversation events since the previous request:")
        for message in messages:
            role = message.get("role", "user").upper()
            parts.append(f"[{role}]\n{json.dumps(message, ensure_ascii=False, indent=2)}")
        return "\n\n".join(parts)

    def submit_prompt(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        trace: DeepSeekTrace | None = None,
    ) -> str:
        if trace is not None:
            trace.mark("context_ready")
        with self._lock:
            if self.force_new_chat:
                self.reset_page()
                self._sticky_initialized = False
                self._sticky_last_messages = []
                self._sticky_messages_since_full = 0
            page = self.ensure_page()
            if trace is not None:
                trace.mark("page_created")
            try:
                self.ensure_chat_ready(page, trace=trace)
                if self.force_new_chat:
                    if trace is not None:
                        trace.set("new_chat_selector", "fresh_page")
                        trace.mark("new_chat_ready")
                elif self.sticky_marker and not self._sticky_initialized:
                    self._sticky_initialized = self.detect_sticky_marker(page)
                    if trace is not None:
                        trace.set("sticky_initialized", self._sticky_initialized)

                prompt_messages = messages
                prompt_builder = self.build_full_prompt
                if not self.force_new_chat and self.sticky_marker and self._sticky_initialized:
                    delta_messages = self.compute_delta_messages(messages)
                    should_reanchor = (
                        self.sticky_reanchor_messages is not None
                        and self._sticky_messages_since_full + len(delta_messages)
                        >= self.sticky_reanchor_messages
                    )
                    if delta_messages and not should_reanchor:
                        prompt_messages = delta_messages
                        prompt_builder = self.build_delta_prompt
                        if trace is not None:
                            trace.set("sticky_mode", "delta")
                            trace.set("delta_message_count", len(delta_messages))
                    elif should_reanchor:
                        if trace is not None:
                            trace.set("sticky_mode", "reanchor_full")
                            trace.set(
                                "sticky_messages_since_full",
                                self._sticky_messages_since_full,
                            )
                            trace.set("delta_message_count", len(delta_messages))
                    else:
                        if trace is not None:
                            trace.set("sticky_mode", "noop_fallback")
                    if trace is not None and trace.values.get("sticky_mode") is None:
                        trace.set("sticky_mode", "full_replay")
                elif trace is not None:
                    trace.set("sticky_mode", "full_init" if self.sticky_marker else "stateless")

                prompt = prompt_builder(messages=prompt_messages, tools=tools)
                try:
                    input_box = self.first_visible(page, self.input_selectors)
                except RuntimeError:
                    logger.debug("DeepSeek input box missing after initial page prep; retrying page load once.")
                    page.goto(self.url, wait_until="domcontentloaded", timeout=self.page_load_timeout_ms)
                    page.wait_for_timeout(1000)
                    self._refresh_current_chat_url(page, persist=False)
                    input_box = self.first_visible(page, self.input_selectors)
                if trace is not None:
                    trace.mark("input_ready")
                assistant_locator = self.assistant_locator(page)
                before_count = assistant_locator.count()
                if self.force_new_chat:
                    before_last_text = self.last_assistant_text(assistant_locator, page)
                    before_last_index = -1
                else:
                    before_snapshot = self.assistant_snapshot(assistant_locator, page)
                    before_last_text = before_snapshot["text"]
                    before_last_index = before_snapshot["index"]
                transport_capture = DeepSeekTransportCapture(page)
                transport_capture.install()
                if trace is not None:
                    trace.set("assistant_count_before", before_count)
                    trace.set("assistant_chars_before", len(before_last_text))
                    trace.set("assistant_index_before", before_last_index)
                try:
                    self.fill_input(input_box, prompt)
                    if trace is not None:
                        trace.mark("prompt_filled")
                    if not self.try_submit(page, input_box, trace=trace):
                        raise RuntimeError("Failed to submit prompt to DeepSeek web UI.")
                    self.scroll_chat_to_bottom(page)
                    if self.sticky_marker:
                        self._sticky_initialized = True
                        self._sticky_last_messages = [dict(message) for message in messages]
                        if not self.force_new_chat and prompt_builder is self.build_delta_prompt:
                            self._sticky_messages_since_full += len(prompt_messages)
                        else:
                            self._sticky_messages_since_full = 0
                    if trace is not None:
                        trace.mark("submitted")
                    response_text = self.wait_for_response(
                        page,
                        before_count,
                        before_last_text,
                        before_last_index,
                        transport_capture=transport_capture,
                        trace=trace,
                    )
                    self._refresh_current_chat_url(page)
                    copied_text = self.try_copy_last_assistant_text(page)
                    if copied_text and (
                        looks_like_tool_payload(copied_text)
                        or len(copied_text) >= len(response_text)
                    ):
                        if trace is not None:
                            trace.set("response_chars", len(copied_text))
                            trace.set("response_ready_reason", "copy_button_post_wait")
                        logger.warning(
                            "DeepSeek submit_prompt replacing response with clipboard copy response_chars=%d copied_chars=%d",
                            len(response_text),
                            len(copied_text),
                        )
                        return copied_text
                    return response_text
                finally:
                    self._save_session_state()
                    transport_capture.close()
            except Exception:
                logger.exception("DeepSeek web bridge request failed.")
                raise

    def parse_model_payload(self, raw_text: str) -> dict[str, Any]:
        try:
            payload = extract_json_object(raw_text)
        except Exception:
            salvaged = salvage_tool_calls_payload(raw_text)
            if salvaged is not None:
                try:
                    SALVAGED_PAYLOAD_DEBUG_PATH.write_text(raw_text, encoding="utf-8")
                except Exception:
                    logger.debug("Failed to persist salvaged DeepSeek payload for debugging.", exc_info=True)
                logger.warning(
                    "DeepSeek web response was malformed JSON. Salvaged tool_calls payload saved to %s.",
                    SALVAGED_PAYLOAD_DEBUG_PATH,
                )
                return salvaged

            try:
                INVALID_PAYLOAD_DEBUG_PATH.write_text(raw_text, encoding="utf-8")
            except Exception:
                logger.debug("Failed to persist invalid DeepSeek payload for debugging.", exc_info=True)

            logger.warning("DeepSeek web response was not valid JSON. Suppressing raw JSON-like text from UI fallback.")
            logger.warning(
                "DeepSeek invalid payload saved to %s (preview=%r)",
                INVALID_PAYLOAD_DEBUG_PATH,
                raw_text.strip()[:800],
            )
            stripped = raw_text.strip()
            if stripped.startswith("{") and '"tool_calls"' in stripped:
                return {"content": "", "tool_calls": [], "raw_text": raw_text}
            return {"content": stripped, "tool_calls": [], "raw_text": raw_text}

        normalized_tool_calls = []
        for item in payload.get("tool_calls", []) or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            arguments = item.get("arguments", {})
            if not isinstance(arguments, dict):
                try:
                    parsed_arguments = json.loads(arguments)
                    if isinstance(parsed_arguments, dict):
                        arguments = parsed_arguments
                    else:
                        arguments = {}
                except Exception:
                    arguments = {}
            normalized_tool_calls.append(
                {
                    "id": item.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    "name": name,
                    "arguments": arguments,
                }
            )

        content = payload.get("content", "")
        if isinstance(content, str):
            stripped = content.strip()
            if stripped.startswith("{") and '"tool_calls"' in stripped:
                content = ""
        return {
            "content": content if isinstance(content, str) else normalize_text_content(content),
            "tool_calls": normalized_tool_calls,
            "raw_text": raw_text,
        }

    def assistant_locator(self, page: Page) -> Locator:
        return page.locator(", ".join(self.assistant_selectors))

    def visible_payload_candidates(self, page: Page, *, limit: int = 24) -> list[dict[str, Any]]:
        try:
            result = page.evaluate(
                """(limit) => {
                    for (const node of document.querySelectorAll('[data-deerflow-json-candidate-id]')) {
                        node.removeAttribute('data-deerflow-json-candidate-id');
                    }

                    const out = [];
                    const seen = new Set();
                    let domIndex = 0;
                    for (const node of document.body.querySelectorAll('*')) {
                        if (!(node instanceof HTMLElement)) {
                            continue;
                        }
                        const style = window.getComputedStyle(node);
                        if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') {
                            continue;
                        }
                        const rect = node.getBoundingClientRect();
                        if (!rect.width || !rect.height) {
                            continue;
                        }
                        const text = (node.innerText || node.textContent || '').trim();
                        if (!text || !text.includes('"tool_calls"')) {
                            continue;
                        }
                        if (seen.has(text)) {
                            domIndex += 1;
                            continue;
                        }
                        seen.add(text);
                        const probeId = String(out.length);
                        node.dataset.deerflowJsonCandidateId = probeId;
                        out.push({
                            probeId,
                            domIndex,
                            text,
                        });
                        domIndex += 1;
                    }
                    return out.slice(-limit);
                }""",
                limit,
            )
        except Exception:
            logger.debug("Failed to inspect visible DeepSeek payload candidates.", exc_info=True)
            return []
        if not isinstance(result, list):
            return []

        normalized: list[dict[str, Any]] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue

            substrings = extract_payload_text_candidates(text)
            try:
                dom_index = int(item.get("domIndex"))
            except Exception:
                dom_index = len(normalized)

            for offset, substring in enumerate(substrings):
                if not substring or '"tool_calls"' not in substring:
                    continue
                normalized.append(
                    {
                        "probeId": item.get("probeId"),
                        "domIndex": (dom_index * 10) + offset,
                        "text": substring,
                    }
                )

        return normalized[-limit:]

    def assistant_text_candidates(self, locator: Locator, *, max_items: int = 24) -> list[dict[str, Any]]:
        count = locator.count()
        if count == 0:
            return []

        start = max(0, count - max_items)
        candidates: list[dict[str, Any]] = []
        for index in range(start, count):
            node = locator.nth(index)
            texts: list[str] = []
            try:
                rendered = node.inner_text(timeout=500)
                if rendered:
                    texts.append(rendered)
            except Exception:
                pass

            try:
                raw = node.text_content(timeout=500)
                if raw:
                    texts.append(raw)
            except Exception:
                pass

            text = choose_best_assistant_text(texts)
            if text:
                candidates.append({"index": index, "text": text})

        return candidates

    def best_assistant_candidate(self, locator: Locator) -> dict[str, Any] | None:
        return choose_best_assistant_candidate(self.assistant_text_candidates(locator))

    def best_assistant_locator(self, locator: Locator) -> Locator | None:
        candidate = self.best_assistant_candidate(locator)
        if candidate is None:
            return None
        return locator.nth(candidate["index"])

    def read_assistant_text(self, locator: Locator) -> str:
        candidate = self.best_assistant_candidate(locator)
        if candidate is None:
            return ""
        return candidate["text"]

    def assistant_snapshot(self, locator: Locator, page: Page | None = None) -> dict[str, Any]:
        payload_candidate: dict[str, Any] | None = None
        if page is not None:
            try:
                payload_candidate = choose_best_payload_candidate(self.visible_payload_candidates(page))
            except Exception:
                payload_candidate = None
        if payload_candidate is not None:
            return {
                "source": "payload",
                "index": int(payload_candidate.get("domIndex", -1)),
                "text": payload_candidate.get("text", "") or "",
            }

        candidate = self.best_assistant_candidate(locator)
        if candidate is None:
            return {"source": "assistant", "index": -1, "text": ""}
        text = candidate.get("text", "") or ""
        if is_schema_example_payload_text(text) or any(marker in text for marker in PROMPT_REPLAY_MARKERS):
            return {"source": "assistant", "index": -1, "text": ""}
        return {
            "source": "assistant",
            "index": int(candidate.get("index", -1)),
            "text": text,
        }

    def reset_copy_capture(self, page: Page) -> None:
        try:
            page.evaluate("window.__deerflowCopyEvents = [];")
        except Exception:
            logger.debug("Failed to reset DeepSeek copy capture state.", exc_info=True)

    def scroll_chat_to_bottom(self, page: Page) -> None:
        try:
            page.evaluate(
                """() => {
                    const seen = new Set();
                    const nodes = [
                        document.scrollingElement,
                        document.documentElement,
                        document.body,
                    ];
                    for (const node of document.querySelectorAll('*')) {
                        if (!(node instanceof HTMLElement)) {
                            continue;
                        }
                        if (node.scrollHeight <= node.clientHeight + 40) {
                            continue;
                        }
                        nodes.push(node);
                    }
                    for (const node of nodes) {
                        if (!node || seen.has(node)) {
                            continue;
                        }
                        seen.add(node);
                        try {
                            node.scrollTop = node.scrollHeight;
                        } catch {}
                    }
                    try {
                        window.scrollTo(0, document.body.scrollHeight || document.documentElement.scrollHeight || 0);
                    } catch {}
                }"""
            )
        except Exception:
            logger.debug("Failed to scroll DeepSeek chat to bottom.", exc_info=True)

    def read_latest_copy_capture(self, page: Page) -> str:
        try:
            latest = page.evaluate(
                """() => {
                    const events = window.__deerflowCopyEvents || [];
                    const item = events[events.length - 1];
                    return item && typeof item.text === 'string' ? item.text : '';
                }"""
            )
        except Exception:
            return ""
        return latest.strip() if isinstance(latest, str) else ""

    def inspect_copy_button_candidates(self, target: Locator, *, limit: int = 24) -> list[dict[str, Any]]:
        try:
            result = target.evaluate(
                """(el, limit) => {
                    const buttonSelector = 'button,[role="button"],div[role="button"]';
                    for (const node of document.querySelectorAll('[data-deerflow-probe-id]')) {
                        node.removeAttribute('data-deerflow-probe-id');
                    }

                    const isVisible = (node) => {
                        if (!(node instanceof HTMLElement)) {
                            return false;
                        }
                        const style = window.getComputedStyle(node);
                        if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') {
                            return false;
                        }
                        const rect = node.getBoundingClientRect();
                        return !!rect.width && !!rect.height;
                    };

                    const target = el instanceof HTMLElement ? el : null;
                    if (!target) {
                        return [];
                    }

                    const targetRect = target.getBoundingClientRect();
                    let parent = target;
                    const actionBars = [];
                    for (let depth = 0; depth < 8 && parent; depth += 1) {
                        for (const child of parent.children) {
                            if (!(child instanceof HTMLElement)) {
                                continue;
                            }
                            if (child === target || child.contains(target)) {
                                continue;
                            }
                            const buttons = [...child.querySelectorAll(buttonSelector)].filter(isVisible);
                            if (buttons.length < 4 || buttons.length > 8) {
                                continue;
                            }
                            const rect = child.getBoundingClientRect();
                            const distance = Math.abs(rect.top - targetRect.bottom) + Math.abs(rect.left - targetRect.left);
                            actionBars.push({
                                child,
                                buttons,
                                distance,
                                depth,
                            });
                        }
                        parent = parent.parentElement;
                    }

                    if (actionBars.length === 0) {
                        return [];
                    }

                    actionBars.sort((a, b) => {
                        if (a.depth !== b.depth) {
                            return a.depth - b.depth;
                        }
                        const aButtonDelta = Math.abs(a.buttons.length - 5);
                        const bButtonDelta = Math.abs(b.buttons.length - 5);
                        if (aButtonDelta !== bButtonDelta) {
                            return aButtonDelta - bButtonDelta;
                        }
                        return a.distance - b.distance;
                    });

                    const out = [];
                    let probeId = 0;
                    for (const actionBar of actionBars) {
                        const orderedButtons = [...actionBar.buttons].sort((a, b) => {
                            const aRect = a.getBoundingClientRect();
                            const bRect = b.getBoundingClientRect();
                            if (aRect.top !== bRect.top) {
                                return aRect.top - bRect.top;
                            }
                            return aRect.left - bRect.left;
                        });
                        for (const button of orderedButtons) {
                            const label = [
                                button.getAttribute('aria-label') || '',
                                button.getAttribute('title') || '',
                                button.innerText || '',
                                button.textContent || '',
                            ].join(' ').trim();
                            const rect = button.getBoundingClientRect();
                            button.dataset.deerflowProbeId = String(probeId);
                            out.push({
                                probeId: String(probeId),
                                label,
                                className: typeof button.className === 'string' ? button.className : '',
                                parentClassName: button.parentElement && typeof button.parentElement.className === 'string'
                                    ? button.parentElement.className
                                    : '',
                                distance: Math.abs(rect.top - targetRect.bottom) + Math.abs(rect.left - targetRect.left),
                                actionBarClassName: typeof actionBar.child.className === 'string' ? actionBar.child.className : '',
                                actionBarDepth: actionBar.depth,
                                actionBarButtonCount: actionBar.buttons.length,
                            });
                            probeId += 1;
                        }
                    }
                    return out.slice(0, limit);
                }""",
                limit,
            )
        except Exception:
            logger.debug("Failed to inspect DeepSeek copy button candidates.", exc_info=True)
            return []
        return result if isinstance(result, list) else []

    def read_hover_texts_near_candidate(self, page: Page, probe_id: str) -> list[str]:
        try:
            locator = page.locator(f'[data-deerflow-probe-id="{probe_id}"]').first
            locator.hover(timeout=1500)
            page.wait_for_timeout(350)
            result = page.evaluate(
                """(probeId) => {
                    const button = document.querySelector(`[data-deerflow-probe-id="${probeId}"]`);
                    if (!(button instanceof HTMLElement)) {
                        return [];
                    }
                    const rect = button.getBoundingClientRect();
                    const centerX = rect.left + rect.width / 2;
                    const centerY = rect.top + rect.height / 2;
                    const out = [];
                    for (const node of document.body.querySelectorAll('*')) {
                        if (!(node instanceof HTMLElement) || node === button || node.contains(button)) {
                            continue;
                        }
                        const style = window.getComputedStyle(node);
                        if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') {
                            continue;
                        }
                        const text = (node.innerText || node.textContent || '').trim();
                        if (!text || text.length > 80) {
                            continue;
                        }
                        const nodeRect = node.getBoundingClientRect();
                        if (!nodeRect.width || !nodeRect.height) {
                            continue;
                        }
                        const nodeCenterX = nodeRect.left + nodeRect.width / 2;
                        const nodeCenterY = nodeRect.top + nodeRect.height / 2;
                        const distance = Math.abs(nodeCenterX - centerX) + Math.abs(nodeCenterY - centerY);
                        if (distance > 220) {
                            continue;
                        }
                        out.push({ text, distance });
                    }
                    out.sort((a, b) => a.distance - b.distance);
                    return out.map((item) => item.text).slice(0, 8);
                }""",
                probe_id,
            )
        except Exception:
            logger.debug("Failed to read DeepSeek hover texts for copy candidate.", exc_info=True)
            return []
        return [item.strip() for item in result if isinstance(item, str) and item.strip()] if isinstance(result, list) else []

    def try_copy_last_assistant_text(self, page: Page) -> str:
        try:
            page.evaluate(COPY_CAPTURE_INIT_SCRIPT)
        except Exception:
            logger.debug("Failed to reinstall DeepSeek copy capture before clicking copy.", exc_info=True)
        self.scroll_chat_to_bottom(page)
        self.reset_copy_capture(page)
        attempts: list[dict[str, Any]] = []
        hover_payload_candidates: list[dict[str, Any]] = []
        locator = self.assistant_locator(page)
        payload_candidates = sorted(
            self.visible_payload_candidates(page),
            key=payload_candidate_score,
            reverse=True,
        )
        assistant_candidates: list[dict[str, Any]] = []
        if payload_candidates:
            for payload_candidate in payload_candidates:
                probe_id = payload_candidate.get("probeId")
                if not isinstance(probe_id, str):
                    continue
                assistant_candidates.append(
                    {
                        "kind": "payload",
                        "probeId": probe_id,
                        "text": payload_candidate.get("text", ""),
                    }
                )
        else:
            for assistant_candidate in sorted(
                self.assistant_text_candidates(locator),
                key=assistant_candidate_score,
                reverse=True,
            ):
                assistant_candidates.append(
                    {
                        "kind": "locator",
                        "index": assistant_candidate.get("index"),
                        "text": assistant_candidate.get("text", ""),
                    }
                )

        for assistant_candidate in assistant_candidates:
            try:
                if assistant_candidate.get("kind") == "payload":
                    target_probe_id = assistant_candidate.get("probeId")
                    if not isinstance(target_probe_id, str):
                        continue
                    target = page.locator(f'[data-deerflow-json-candidate-id="{target_probe_id}"]').first
                else:
                    target_index = assistant_candidate.get("index")
                    if not isinstance(target_index, int):
                        continue
                    target = locator.nth(target_index)
                target.hover(timeout=1500)
                page.wait_for_timeout(150)
            except Exception:
                logger.debug("Failed to hover candidate DeepSeek assistant message before copy.", exc_info=True)
                continue

            copy_button_candidates = self.inspect_copy_button_candidates(target, limit=24)
            for candidate in copy_button_candidates:
                probe_id = candidate.get("probeId")
                if not isinstance(probe_id, str):
                    continue
                hover_texts = self.read_hover_texts_near_candidate(page, probe_id)
                for hover_text in hover_texts:
                    if looks_like_assistant_payload_candidate(hover_text):
                        hover_payload_candidates.append(
                            {
                                "probeId": probe_id,
                                "domIndex": len(hover_payload_candidates),
                                "text": hover_text,
                            }
                        )
                lowered = {text.strip().lower() for text in hover_texts}
                attempts.append(
                    {
                        "assistantKind": assistant_candidate.get("kind"),
                        "assistantIndex": assistant_candidate.get("index"),
                        "assistantProbeId": assistant_candidate.get("probeId"),
                        "assistantPreview": assistant_candidate.get("text", "")[:120],
                        "probeId": probe_id,
                        "distance": candidate.get("distance"),
                        "className": candidate.get("className"),
                        "actionBarDepth": candidate.get("actionBarDepth"),
                        "actionBarButtonCount": candidate.get("actionBarButtonCount"),
                        "hoverTexts": hover_texts,
                    }
                )
                if "copy" not in lowered and "复制" not in lowered:
                    continue
                try:
                    button = page.locator(f'[data-deerflow-probe-id="{probe_id}"]').first
                    button.click(timeout=1500)
                    page.wait_for_timeout(600)
                except Exception:
                    logger.debug("Failed to click DeepSeek copy candidate.", exc_info=True)
                    continue
                copied = self.read_latest_copy_capture(page)
                if copied:
                    copied_payload = choose_best_payload_candidate(
                        [
                            {
                                "probeId": probe_id,
                                "domIndex": index,
                                "text": candidate_text,
                            }
                            for index, candidate_text in enumerate(extract_payload_text_candidates(copied))
                        ]
                    )
                    if copied_payload is not None:
                        copied = copied_payload["text"]
                    elif not looks_like_assistant_payload_candidate(copied) or is_schema_example_payload_text(copied):
                        continue
                    logger.warning(
                        "DeepSeek copy capture succeeded assistant_kind=%s assistant_index=%s assistant_probe_id=%s probe_id=%s distance=%s copied_chars=%d hover_texts=%s",
                        assistant_candidate.get("kind"),
                        assistant_candidate.get("index"),
                        assistant_candidate.get("probeId"),
                        probe_id,
                        candidate.get("distance"),
                        len(copied),
                        hover_texts,
                    )
                    return copied

        hover_payload = choose_best_payload_candidate(hover_payload_candidates)
        if hover_payload is not None:
            logger.warning(
                "DeepSeek copy capture fell back to hover payload probe_id=%s payload_chars=%d",
                hover_payload.get("probeId"),
                len(hover_payload["text"]),
            )
            return hover_payload["text"]

        logger.warning("DeepSeek copy capture did not fire. attempts=%s", attempts)
        return ""

    def last_assistant_text(self, locator: Locator, page: Page | None = None) -> str:
        if page is not None:
            try:
                payload_candidate = choose_best_payload_candidate(self.visible_payload_candidates(page))
            except Exception:
                payload_candidate = None
            if payload_candidate is not None:
                return payload_candidate["text"]
        try:
            fallback = self.read_assistant_text(locator)
        except Exception:
            return ""
        if not fallback:
            return ""
        if is_schema_example_payload_text(fallback):
            return ""
        if any(marker in fallback for marker in PROMPT_REPLAY_MARKERS):
            return ""
        return fallback

    def detect_sticky_marker(self, page: Page) -> bool:
        if not self.sticky_marker:
            return False
        try:
            body_text = page.locator("body").inner_text(timeout=1500)
        except Exception:
            return False
        if not body_text:
            return False
        return self.sticky_marker in body_text[-self.sticky_scan_chars :]

    def compute_delta_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        previous = self._sticky_last_messages
        prefix_len = 0
        for old, new in zip(previous, messages):
            if old == new:
                prefix_len += 1
            else:
                break

        delta = messages[prefix_len:]
        if delta:
            return delta
        if messages:
            return [messages[-1]]
        return []

    def best_effort_start_new_chat(self, page: Page, *, trace: DeepSeekTrace | None = None) -> None:
        """Try to reset the DeepSeek page to a fresh conversation.

        The bridge is intended to be stateless: DeerFlow sends the effective
        transcript every request. Reusing an existing DeepSeek web thread can
        duplicate context or leak cross-thread memory, so we best-effort click
        the site's "new chat" entry before submitting.
        """
        for selector in self.new_chat_selectors:
            locator = page.locator(selector).last
            try:
                locator.wait_for(state="visible", timeout=800)
                locator.click(timeout=1200)
                page.wait_for_timeout(500)
                if trace is not None:
                    trace.set("new_chat_selector", selector)
                    trace.mark("new_chat_ready")
                return
            except PlaywrightError:
                continue
            except PlaywrightTimeoutError:
                continue
        logger.debug("DeepSeek new chat button not found; continuing on current page.")
        if trace is not None:
            trace.set("new_chat_selector", None)
            trace.mark("new_chat_ready")

    def first_visible(self, page: Page, selectors: tuple[str, ...]) -> Locator:
        for selector in selectors:
            locator = page.locator(selector).last
            try:
                locator.wait_for(state="visible", timeout=1500)
                return locator
            except PlaywrightTimeoutError:
                continue
        raise RuntimeError(
            "DeepSeek input box not found. Please login in the persistent browser profile and keep the page on chat view."
        )

    def fill_input(self, input_box: Locator, prompt: str) -> None:
        input_box.click()
        tag_name = input_box.evaluate("(node) => node.tagName.toLowerCase()")
        if tag_name == "textarea":
            input_box.fill(prompt)
            return
        input_box.evaluate(
            """(node, value) => {
                node.textContent = value;
                node.dispatchEvent(new Event('input', { bubbles: true }));
            }""",
            prompt,
        )

    def try_submit(self, page: Page, input_box: Locator, *, trace: DeepSeekTrace | None = None) -> bool:
        for selector in self.send_selectors:
            button = page.locator(selector).last
            if button.count() == 0:
                continue
            try:
                button.click(timeout=1200)
                if trace is not None:
                    trace.set("submit_selector", selector)
                return True
            except PlaywrightError:
                continue
        try:
            input_box.press("Enter")
            if trace is not None:
                trace.set("submit_selector", "Enter")
            return True
        except PlaywrightError:
            return False

    def can_submit_next_turn(self, page: Page) -> bool:
        for selector in self.send_selectors:
            button = page.locator(selector).last
            try:
                if button.count() == 0:
                    continue
                if button.is_visible(timeout=200) and button.is_enabled(timeout=200):
                    return True
            except Exception:
                continue
        return False

    def wait_for_response(
        self,
        page: Page,
        before_count: int,
        before_last_text: str,
        before_last_index: int,
        *,
        transport_capture: DeepSeekTransportCapture | None = None,
        trace: DeepSeekTrace | None = None,
    ) -> str:
        locator = self.assistant_locator(page)
        deadline = time.time() + (self.response_timeout_ms / 1000)
        last_progress_log = 0.0
        generation_busy_seen = False

        while time.time() < deadline:
            self.scroll_chat_to_bottom(page)
            current_count = locator.count()
            if self.force_new_chat:
                current_snapshot = None
                current_last_text = self.last_assistant_text(locator, page)
            else:
                current_snapshot = self.assistant_snapshot(locator, page)
                current_last_text = current_snapshot["text"]
            transport_text = transport_capture.best_payload_text() if transport_capture is not None else ""
            can_submit = self.can_submit_next_turn(page)
            if not can_submit:
                generation_busy_seen = True

            if self.force_new_chat:
                assistant_started = (
                    current_count > before_count
                    or (
                        generation_busy_seen
                        and current_count > 0
                        and current_last_text
                        and current_last_text != before_last_text
                    )
                )
            else:
                assistant_started = (
                    current_snapshot["index"] > before_last_index
                    or (current_last_text and current_last_text != before_last_text)
                )

            if assistant_started or transport_text:
                if trace is not None:
                    trace.set("assistant_count_after_start", current_count)
                    trace.set("assistant_chars_after_start", len(current_last_text))
                    trace.set(
                        "assistant_index_after_start",
                        current_snapshot["index"] if current_snapshot is not None else -1,
                    )
                    trace.set("generation_busy_seen", generation_busy_seen)
                    if transport_capture is not None:
                        trace.set("transport_candidate_count", transport_capture.candidate_count)
                        trace.set("transport_chars_after_start", len(transport_text))
                    trace.mark("response_started")
                break
            page.wait_for_timeout(300)
        else:
            raise TimeoutError("Timed out waiting for DeepSeek response to appear.")

        stable_seen = 0
        last_text = ""
        stable_transport_seen = 0
        last_transport_text = ""
        tool_payload_stable_rounds = max(self.stable_rounds, 10)

        def assistant_has_advanced(current_count: int, current_index: int, current_text: str) -> bool:
            if self.force_new_chat:
                if current_count > before_count:
                    return True
                if current_text and not before_last_text:
                    return True
                return bool(current_text) and current_text != before_last_text
            if current_index > before_last_index:
                return True
            if current_count > before_count:
                return True
            if current_text and not before_last_text:
                return True
            return bool(current_text) and current_text != before_last_text

        while time.time() < deadline:
            self.scroll_chat_to_bottom(page)
            transport_text = transport_capture.best_payload_text() if transport_capture is not None else ""
            if transport_text:
                if is_assistant_payload_text(transport_text):
                    if trace is not None:
                        trace.set("response_chars", len(transport_text))
                        trace.set("response_ready_reason", "transport_assistant_payload")
                        trace.set("transport_candidate_count", transport_capture.candidate_count)
                        trace.mark("response_stable")
                    return transport_text

            if transport_text and transport_text == last_transport_text:
                stable_transport_seen += 1
            else:
                stable_transport_seen = 0
                last_transport_text = transport_text

            current_count = locator.count()
            if self.force_new_chat:
                current_snapshot = None
                current = self.last_assistant_text(locator, page)
                current_index = -1
            else:
                current_snapshot = self.assistant_snapshot(locator, page)
                current = current_snapshot["text"]
                current_index = current_snapshot["index"]
            has_advanced = assistant_has_advanced(current_count, current_index, current)
            if current:
                if has_advanced and self.can_submit_next_turn(page):
                    copied = self.try_copy_last_assistant_text(page)
                    if copied:
                        if trace is not None:
                            trace.set("response_chars", len(copied))
                            trace.set("response_ready_reason", "copy_button")
                            trace.mark("response_stable")
                        logger.warning(
                            "DeepSeek wait_for_response returning clipboard copy assistant_chars=%d copied_chars=%d transport_candidates=%d",
                            len(current),
                            len(copied),
                            transport_capture.candidate_count if transport_capture is not None else 0,
                        )
                        return copied
                if has_advanced:
                    try:
                        extract_json_object(current)
                        if trace is not None:
                            trace.set("response_chars", len(current))
                            trace.set("response_ready_reason", "json_parseable")
                            trace.mark("response_stable")
                        return current
                    except Exception:
                        pass
                if has_advanced and looks_like_assistant_payload_candidate(current) and self.can_submit_next_turn(page):
                    if trace is not None:
                        trace.set("response_chars", len(current))
                        trace.set("response_ready_reason", "tool_payload_submit_ready")
                        trace.mark("response_stable")
                    logger.warning(
                        "DeepSeek wait_for_response returning when submit became ready assistant_chars=%d transport_candidates=%d",
                        len(current),
                        transport_capture.candidate_count if transport_capture is not None else 0,
                    )
                    return current

            if current and current == last_text:
                stable_seen += 1
            else:
                stable_seen = 0
                last_text = current
            now = time.time()
            if now - last_progress_log >= 5:
                logger.warning(
                    "DeepSeek wait_for_response progress assistant_chars=%d stable_seen=%d transport_chars=%d transport_stable_seen=%d transport_candidates=%d",
                    len(current),
                    stable_seen,
                    len(transport_text),
                    stable_transport_seen,
                    transport_capture.candidate_count if transport_capture is not None else 0,
                )
                last_progress_log = now
            if transport_text and stable_transport_seen >= self.stable_rounds:
                if trace is not None:
                    trace.set("response_chars", len(transport_text))
                    trace.set("response_ready_reason", "transport_stable_text")
                    trace.set("transport_candidate_count", transport_capture.candidate_count if transport_capture else 0)
                    trace.mark("response_stable")
                return transport_text
            if has_advanced and current and stable_seen >= self.stable_rounds:
                if looks_like_assistant_payload_candidate(current):
                    copied = self.try_copy_last_assistant_text(page)
                    if copied:
                        if trace is not None:
                            trace.set("response_chars", len(copied))
                            trace.set("response_ready_reason", "copy_button_stable_text")
                            trace.mark("response_stable")
                        return copied
                    if not self.can_submit_next_turn(page) and stable_seen < tool_payload_stable_rounds:
                        page.wait_for_timeout(min(self.stable_poll_interval_ms, 200))
                        continue
                if trace is not None:
                    trace.set("response_chars", len(current))
                    trace.set("response_ready_reason", "stable_text")
                    trace.mark("response_stable")
                return current
            page.wait_for_timeout(min(self.stable_poll_interval_ms, 200))

        raise TimeoutError("Timed out waiting for DeepSeek response to stabilize.")


def langchain_messages_to_gateway_payload(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            payload.append({"role": "system", "content": normalize_text_content(message.content)})
        elif isinstance(message, HumanMessage):
            payload.append({"role": "user", "content": normalize_text_content(message.content)})
        elif isinstance(message, ToolMessage):
            payload.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": normalize_text_content(message.content),
                }
            )
        elif isinstance(message, AIMessage):
            item: dict[str, Any] = {"role": "assistant", "content": normalize_text_content(message.content)}
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": tool_call.get("id"),
                        "name": tool_call.get("name"),
                        "arguments": tool_call.get("args", {}),
                    }
                    for tool_call in message.tool_calls
                ]
            payload.append(item)
        else:
            payload.append({"role": "user", "content": normalize_text_content(message.content)})
    return payload
