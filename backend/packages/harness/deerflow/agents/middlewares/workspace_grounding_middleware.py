"""Middleware that enforces evidence-first workspace exploration.

This guards against two common DeepSeek Web failure modes observed in live runs:
1. answering workspace/file/code questions without using tools first
2. ignoring fresh tool results and reusing stale earlier guesses
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.loop_detection_middleware import (
    LOOP_CONTROL_KEY,
    LOOP_CONTROL_MESSAGE_NAME,
)
from deerflow.agents.thread_state import ThreadState

_WORKSPACE_CONTEXT_MESSAGE_NAME = "workspace_context"
_WORKSPACE_GROUNDING_MESSAGE_NAME = "workspace_grounding"
_DISCOVERY_TOOLS = {"ls", "glob", "grep"}
_CONTENT_TOOLS = {"read_file", "view_image"}
_READ_EVIDENCE_TOOLS = _DISCOVERY_TOOLS | _CONTENT_TOOLS
_WORKSPACE_EVIDENCE_RE = re.compile(
    r"("
    r"\b(file|files|folder|folders|directory|directories|workspace|project|repo|repository|"
    r"source|code|readme|prompt|config|log|logs|pdf|document|documents|doc|docs|excel|csv|"
    r"search|find|list|show|read|analy[sz]e|inspect|explain|summari[sz]e|research|grep|glob|"
    r"module|modules|structure)\b"
    r"|"
    r"\.[a-z0-9]{1,8}\b"
    r"|"
    r"(文件|目录|工作区|项目|仓库|源码|代码|提示词|配置|日志|文档|表格|搜索|查找|列出|看下|看看|读取|分析|解答|解释|总结|研究|模块|结构|压缩|上下文|pdf|excel|csv|readme)"
    r")",
    re.IGNORECASE,
)
_DEEP_WORK_RE = re.compile(
    r"("
    r"\b(analy[sz]e|summari[sz]e|explain|understand|modify|edit|fix|refactor|implement|debug|research|update|change)\b"
    r"|"
    r"(分析|总结|解释|理解|修改|编辑|修复|重构|实现|调试|研究|更新|改)"
    r")",
    re.IGNORECASE,
)
_EDIT_WORK_RE = re.compile(
    r"("
    r"\b(modify|edit|fix|refactor|implement|update|change|rename|rewrite|patch)\b"
    r"|"
    r"(修改|编辑|修复|重构|实现|更新|改名|替换|回写|写回|改成|同步修改)"
    r")",
    re.IGNORECASE,
)
_WRITE_EVIDENCE_TOOLS = {"write_file", "str_replace", "bash"}


def _message_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_message_to_text(item) for item in content if item is not None)
    if isinstance(content, dict):
        for key in ("text", "content"):
            value = content.get(key)
            if value is not None:
                return _message_to_text(value)
    return str(content)


def _is_hidden_message(message: BaseMessage) -> bool:
    additional_kwargs = getattr(message, "additional_kwargs", {}) or {}
    if additional_kwargs.get("hide_from_ui") is True:
        return True
    return getattr(message, "name", None) in {
        _WORKSPACE_CONTEXT_MESSAGE_NAME,
        _WORKSPACE_GROUNDING_MESSAGE_NAME,
    }


def _is_loop_control_message(message: BaseMessage) -> bool:
    if not isinstance(message, HumanMessage):
        return False
    additional_kwargs = getattr(message, "additional_kwargs", {}) or {}
    return getattr(message, "name", None) == LOOP_CONTROL_MESSAGE_NAME or bool(additional_kwargs.get(LOOP_CONTROL_KEY))


def _find_last_visible_user_index(messages: list[BaseMessage]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, HumanMessage) and not _is_hidden_message(message):
            return index
    return None


def _is_workspace_evidence_request(message: BaseMessage) -> bool:
    if not isinstance(message, HumanMessage):
        return False
    return bool(_WORKSPACE_EVIDENCE_RE.search(_message_to_text(message.content)))


def _is_deep_workspace_request(message: BaseMessage) -> bool:
    if not isinstance(message, HumanMessage):
        return False
    text = _message_to_text(message.content)
    return bool(_WORKSPACE_EVIDENCE_RE.search(text) and _DEEP_WORK_RE.search(text))


def _is_edit_workspace_request(message: BaseMessage) -> bool:
    if not isinstance(message, HumanMessage):
        return False
    text = _message_to_text(message.content)
    return bool(_WORKSPACE_EVIDENCE_RE.search(text) and _EDIT_WORK_RE.search(text))


def _has_read_evidence_after(messages: list[BaseMessage], start_index: int) -> bool:
    return any(
        isinstance(message, ToolMessage) and getattr(message, "name", None) in _READ_EVIDENCE_TOOLS
        for message in messages[start_index + 1 :]
    )


def _has_discovery_evidence_after(messages: list[BaseMessage], start_index: int) -> bool:
    return any(
        isinstance(message, ToolMessage) and getattr(message, "name", None) in _DISCOVERY_TOOLS
        for message in messages[start_index + 1 :]
    )


def _has_content_evidence_after(messages: list[BaseMessage], start_index: int) -> bool:
    return any(
        isinstance(message, ToolMessage) and getattr(message, "name", None) in _CONTENT_TOOLS
        for message in messages[start_index + 1 :]
    )


def _has_write_evidence_after(messages: list[BaseMessage], start_index: int) -> bool:
    return any(
        isinstance(message, ToolMessage) and getattr(message, "name", None) in _WRITE_EVIDENCE_TOOLS
        for message in messages[start_index + 1 :]
    )


def _has_grounding_message_after(messages: list[BaseMessage], start_index: int) -> bool:
    return any(
        isinstance(message, HumanMessage) and getattr(message, "name", None) == _WORKSPACE_GROUNDING_MESSAGE_NAME
        for message in messages[start_index + 1 :]
    )


def _has_loop_control_after(messages: list[BaseMessage], start_index: int) -> bool:
    return any(_is_loop_control_message(message) for message in messages[start_index + 1 :])


class WorkspaceGroundingMiddleware(AgentMiddleware[ThreadState]):
    """Force workspace evidence collection before answering file/code questions."""

    state_schema = ThreadState

    def _has_bound_workspace(self, state: ThreadState) -> bool:
        thread_data = state.get("thread_data") or {}
        workspace_path = thread_data.get("workspace_path")
        workspace_container_path = thread_data.get("workspace_container_path")
        return isinstance(workspace_path, str) and bool(workspace_path.strip()) or isinstance(workspace_container_path, str) and bool(workspace_container_path.strip())

    def _build_explore_message(self) -> HumanMessage:
        return HumanMessage(
            name=_WORKSPACE_GROUNDING_MESSAGE_NAME,
            additional_kwargs={"hide_from_ui": True},
            content=(
                "<system_reminder>\n"
                "For the latest user request, do not answer from memory or prior guesses.\n"
                "Inspect the bound workspace first using direct local read tools such as `ls`, `glob`, `grep`, or `read_file`.\n"
                "Prefer direct workspace tools over `ask_clarification` or `task` for the first exploration step when the workspace likely contains the answer.\n"
                "If one tool result is insufficient, call another tool instead of guessing.\n"
                "</system_reminder>"
            ),
        )

    def _build_grounded_answer_message(self) -> HumanMessage:
        return HumanMessage(
            name=_WORKSPACE_GROUNDING_MESSAGE_NAME,
            additional_kwargs={"hide_from_ui": True},
            content=(
                "<system_reminder>\n"
                "The latest workspace tool output is authoritative for the current request.\n"
                "Base your next answer strictly on the most recent tool results.\n"
                "Quote exact filenames, paths, or extracted text from those results when relevant.\n"
                "If the latest tool results are not enough, call another tool instead of reusing earlier assumptions.\n"
                "</system_reminder>"
            ),
        )

    def _build_read_before_conclude_message(self) -> HumanMessage:
        return HumanMessage(
            name=_WORKSPACE_GROUNDING_MESSAGE_NAME,
            additional_kwargs={"hide_from_ui": True},
            content=(
                "<system_reminder>\n"
                "You currently only have discovery/search results such as directory listings or grep matches.\n"
                "Before you summarize, explain, or modify implementation details, read the most relevant file(s) with `read_file`.\n"
                "Use the search results to choose the highest-signal files and line ranges, then continue.\n"
                "</system_reminder>"
            ),
        )

    def _build_write_before_conclude_message(self) -> HumanMessage:
        return HumanMessage(
            name=_WORKSPACE_GROUNDING_MESSAGE_NAME,
            additional_kwargs={"hide_from_ui": True},
            content=(
                "<system_reminder>\n"
                "This request asks for real file modifications in the workspace.\n"
                "Do not claim edits are done unless you actually call write tools first (`write_file`/`str_replace`, or `bash` when appropriate),\n"
                "then `read_file` the changed files to verify final content.\n"
                "If no write tool has been used yet, make the required edit tool call now.\n"
                "</system_reminder>"
            ),
        )

    def _evaluate_messages(self, messages: list[BaseMessage]) -> tuple[int | None, bool]:
        user_index = _find_last_visible_user_index(messages)
        if user_index is None:
            return None, False
        user_message = messages[user_index]
        if not _is_workspace_evidence_request(user_message):
            return user_index, False
        return user_index, True

    def _inject_message(self, state: ThreadState) -> dict[str, Any] | None:
        if not self._has_bound_workspace(state):
            return None

        messages = state.get("messages") or []
        user_index, is_workspace_request = self._evaluate_messages(messages)
        if user_index is None or not is_workspace_request:
            return None

        user_message = messages[user_index]
        if _has_loop_control_after(messages, user_index):
            return None
        has_discovery = _has_discovery_evidence_after(messages, user_index)
        has_content = _has_content_evidence_after(messages, user_index)
        has_write = _has_write_evidence_after(messages, user_index)
        is_edit_request = _is_edit_workspace_request(user_message)

        if is_edit_request and has_content and not has_write:
            last_message = messages[-1] if messages else None
            if isinstance(last_message, ToolMessage):
                return {"messages": [self._build_write_before_conclude_message()]}
            return None

        if has_content:
            last_message = messages[-1] if messages else None
            if isinstance(last_message, ToolMessage) and getattr(last_message, "name", None) in _READ_EVIDENCE_TOOLS:
                return {"messages": [self._build_grounded_answer_message()]}
            return None

        if has_discovery:
            last_message = messages[-1] if messages else None
            if isinstance(last_message, ToolMessage) and getattr(last_message, "name", None) in _DISCOVERY_TOOLS:
                if _is_deep_workspace_request(user_message):
                    return {"messages": [self._build_read_before_conclude_message()]}
                return {"messages": [self._build_grounded_answer_message()]}
            return None

        if _has_grounding_message_after(messages, user_index):
            return None

        return {"messages": [self._build_explore_message()]}

    def _extract_tool_name(self, tool: Any) -> str | None:
        if tool is None:
            return None
        if isinstance(tool, dict):
            name = tool.get("name")
            if isinstance(name, str) and name:
                return name
            function = tool.get("function")
            if isinstance(function, dict):
                fn_name = function.get("name")
                if isinstance(fn_name, str) and fn_name:
                    return fn_name
            return None
        name = getattr(tool, "name", None)
        if isinstance(name, str) and name:
            return name
        function = getattr(tool, "function", None)
        fn_name = getattr(function, "name", None) if function is not None else None
        if isinstance(fn_name, str) and fn_name:
            return fn_name
        return None

    def _has_write_tool_available(self, request: ModelRequest) -> bool:
        for tool in request.tools or []:
            name = self._extract_tool_name(tool)
            if isinstance(name, str) and name in _WRITE_EVIDENCE_TOOLS:
                return True
        return False

    def _requires_initial_tool_call(self, request: ModelRequest) -> bool:
        state = request.state or {}
        if not self._has_bound_workspace(state):  # type: ignore[arg-type]
            return False

        messages = list(request.messages or [])
        user_index, is_workspace_request = self._evaluate_messages(messages)
        if user_index is None or not is_workspace_request:
            return False
        if _has_loop_control_after(messages, user_index):
            return False
        user_message = messages[user_index]
        has_discovery = _has_discovery_evidence_after(messages, user_index)
        has_content = _has_content_evidence_after(messages, user_index)
        has_write = _has_write_evidence_after(messages, user_index)
        is_edit_request = _is_edit_workspace_request(user_message)
        if has_content:
            if is_edit_request and not has_write and self._has_write_tool_available(request):
                return True
            return False
        if not request.tools:
            return False
        if has_discovery:
            if is_edit_request and not has_write and self._has_write_tool_available(request):
                return True
            return _is_deep_workspace_request(user_message)
        return True

    @override
    def before_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:  # noqa: ARG002
        return self._inject_message(state)

    @override
    async def abefore_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:  # noqa: ARG002
        return self._inject_message(state)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        if self._requires_initial_tool_call(request):
            request = request.override(tool_choice="required")
        return handler(request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        if self._requires_initial_tool_call(request):
            request = request.override(tool_choice="required")
        return await handler(request)
