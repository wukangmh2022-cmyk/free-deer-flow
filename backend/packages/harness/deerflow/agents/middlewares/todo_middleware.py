"""Middleware that extends TodoListMiddleware with context-loss detection.

When the message history is truncated (e.g., by SummarizationMiddleware), the
original `write_todos` tool call and its ToolMessage can be scrolled out of the
active context window. This middleware detects that situation and injects a
reminder message so the model still knows about the outstanding todo list.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents.middleware import TodoListMiddleware
from langchain.agents.middleware.todo import PlanningState, Todo
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.runtime import Runtime

_TODO_REMINDER_NAME = "todo_reminder"
_TODO_BOOTSTRAP_NAME = "todo_bootstrap_reminder"
_TASK_STEP_RE = re.compile(
    r"(^|\n)\s*(\d+\.\s+|[-*]\s+)",
    re.IGNORECASE,
)
_TASK_EDIT_ACTION_RE = re.compile(
    r"("
    r"\b(modify|edit|fix|refactor|implement|update|change|rename|rewrite|patch)\b"
    r"|"
    r"(修改|编辑|修复|重构|实现|更新|改名|替换|回写|写回|改成|同步修改)"
    r")",
    re.IGNORECASE,
)
_TASK_PLANNING_RE = re.compile(
    r"("
    r"\b(plan|todo|steps?|multi[- ]step|research|analy[sz]e|debug)\b"
    r"|"
    r"(步骤|计划|待办|任务|调研|分析|调试)"
    r")",
    re.IGNORECASE,
)
_TASK_VERIFICATION_RE = re.compile(
    r"("
    r"\b(test|verify|validation|assert|lint|typecheck|read back|summari[sz]e)\b"
    r"|"
    r"(测试|验证|校验|断言|回读|确认|总结修改结果|不要创建新文件)"
    r")",
    re.IGNORECASE,
)
_TASK_CROSS_FILE_RE = re.compile(
    r"("
    r"\b(sync|also update|and update|across files|multiple files)\b"
    r"|"
    r"(同步修改|同时修改|多个文件|调用点|实现和调用)"
    r")",
    re.IGNORECASE,
)
_SIMPLE_READ_ONLY_RE = re.compile(
    r"("
    r"\b(list|show|what files|directory|who are you|hello)\b"
    r"|"
    r"(看下目录|看看目录|有哪些文件|列出文件|你是谁|你好)"
    r"|"
    r"^\s*(ls|pwd|whoami)\s*$"
    r")",
    re.IGNORECASE,
)
_FILE_PATH_RE = re.compile(r"\b[\w./-]+\.[a-zA-Z0-9]{1,12}\b")


def _todos_in_messages(messages: list[Any]) -> bool:
    """Return True if any AIMessage in *messages* contains a write_todos tool call."""
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.get("name") == "write_todos":
                    return True
    return False


def _reminder_in_messages(messages: list[Any]) -> bool:
    """Return True if a todo reminder HumanMessage is already present in *messages*."""
    for msg in messages:
        if isinstance(msg, HumanMessage) and getattr(msg, "name", None) in {
            _TODO_REMINDER_NAME,
            _TODO_BOOTSTRAP_NAME,
        }:
            return True
    return False


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


def _find_last_visible_user_index(messages: list[BaseMessage]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if isinstance(msg, HumanMessage) and getattr(msg, "name", None) not in {
            _TODO_REMINDER_NAME,
            _TODO_BOOTSTRAP_NAME,
        }:
            return index
    return None


def _has_write_todos_evidence_after(messages: list[BaseMessage], start_index: int) -> bool:
    for msg in messages[start_index + 1 :]:
        if isinstance(msg, AIMessage):
            for tool_call in msg.tool_calls or []:
                if tool_call.get("name") == "write_todos":
                    return True
        if isinstance(msg, ToolMessage) and getattr(msg, "name", None) == "write_todos":
            return True
    return False


def _is_complex_request(message: BaseMessage) -> bool:
    if not isinstance(message, HumanMessage):
        return False
    text = _message_to_text(message.content)
    if _SIMPLE_READ_ONLY_RE.search(text):
        return False

    score = 0
    step_count = len(_TASK_STEP_RE.findall(text))
    if step_count >= 2:
        score += 2
    elif step_count == 1:
        score += 1

    if _TASK_EDIT_ACTION_RE.search(text):
        score += 2
    if _TASK_PLANNING_RE.search(text):
        score += 1
    if _TASK_VERIFICATION_RE.search(text):
        score += 1
    if _TASK_CROSS_FILE_RE.search(text):
        score += 1

    file_mentions = set(_FILE_PATH_RE.findall(text))
    if len(file_mentions) >= 2:
        score += 2
    elif len(file_mentions) == 1:
        score += 1

    # Closer to Claude Code behavior:
    # - Do not bootstrap todos for tiny/simple asks.
    # - Bootstrap when complexity signals accumulate.
    return score >= 3


def _format_todos(todos: list[Todo]) -> str:
    """Format a list of Todo items into a human-readable string."""
    lines: list[str] = []
    for todo in todos:
        status = todo.get("status", "pending")
        content = todo.get("content", "")
        lines.append(f"- [{status}] {content}")
    return "\n".join(lines)


class TodoMiddleware(TodoListMiddleware):
    """Extends TodoListMiddleware with `write_todos` context-loss detection."""

    @override
    def before_model(
        self,
        state: PlanningState,
        runtime: Runtime,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        """Inject reminder for truncated todo state and bootstrap plan mode when needed."""
        messages = state.get("messages") or []
        todos: list[Todo] = state.get("todos") or []  # type: ignore[assignment]

        if not todos:
            if _reminder_in_messages(messages):
                return None
            last_user_index = _find_last_visible_user_index(messages)
            if last_user_index is None:
                return None
            user_message = messages[last_user_index]
            if not _is_complex_request(user_message):
                return None
            if _has_write_todos_evidence_after(messages, last_user_index):
                return None
            return {
                "messages": [
                    HumanMessage(
                        name=_TODO_BOOTSTRAP_NAME,
                        additional_kwargs={"hide_from_ui": True},
                        content=(
                            "<system_reminder>\n"
                            "This appears to be a multi-step task in plan mode.\n"
                            "Before proceeding with more tool calls or conclusions, call `write_todos` once to create the task list,\n"
                            "set the first task to `in_progress`, then continue execution.\n"
                            "</system_reminder>"
                        ),
                    )
                ]
            }

        if _todos_in_messages(messages):
            return None
        if _reminder_in_messages(messages):
            return None

        formatted = _format_todos(todos)
        reminder = HumanMessage(
            name=_TODO_REMINDER_NAME,
            content=(
                "<system_reminder>\n"
                "Your todo list from earlier is no longer visible in the current context window, "
                "but it is still active. Here is the current state:\n\n"
                f"{formatted}\n\n"
                "Continue tracking and updating this todo list as you work. "
                "Call `write_todos` whenever the status of any item changes.\n"
                "</system_reminder>"
            ),
        )
        return {"messages": [reminder]}

    @override
    async def abefore_model(
        self,
        state: PlanningState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        """Async version of before_model."""
        return self.before_model(state, runtime)

    def _has_write_todos_tool_available(self, request: ModelRequest) -> bool:
        for tool in request.tools or []:
            if isinstance(tool, dict):
                if tool.get("name") == "write_todos":
                    return True
                function = tool.get("function")
                if isinstance(function, dict) and function.get("name") == "write_todos":
                    return True
            else:
                if getattr(tool, "name", None) == "write_todos":
                    return True
                function = getattr(tool, "function", None)
                if function is not None and getattr(function, "name", None) == "write_todos":
                    return True
        return False

    def _should_force_todo_creation(self, request: ModelRequest) -> bool:
        state = request.state or {}
        todos: list[Todo] = state.get("todos") or []  # type: ignore[assignment]
        if todos:
            return False
        messages = list(request.messages or [])
        last_user_index = _find_last_visible_user_index(messages)
        if last_user_index is None:
            return False
        user_message = messages[last_user_index]
        if not _is_complex_request(user_message):
            return False
        if _has_write_todos_evidence_after(messages, last_user_index):
            return False
        return self._has_write_todos_tool_available(request)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        if self._should_force_todo_creation(request):
            request = request.override(tool_choice="required")
        return handler(request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        if self._should_force_todo_creation(request):
            request = request.override(tool_choice="required")
        return await handler(request)
