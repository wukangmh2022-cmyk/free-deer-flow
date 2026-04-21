"""Lightweight validation for unsupported completion or tool-use claims.

This middleware is intentionally gentle: it does not hard-fail the run or
introduce a second judge model. Instead, it looks for obviously suspicious
assistant replies such as "done", "created", or "I used read_file" when the
current run does not contain matching tool evidence, then injects a hidden
reminder so the model can correct course.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.loop_detection_middleware import (
    LOOP_CONTROL_KEY,
    LOOP_CONTROL_MESSAGE_NAME,
)
from deerflow.agents.thread_state import ThreadState

logger = logging.getLogger(__name__)

COMPLETION_VALIDATION_MESSAGE_NAME = "completion_validation"
COMPLETION_VALIDATION_KEY = "completion_validation"
COMPLETION_VALIDATION_WARNING = "warning"

_WRITE_EVIDENCE_TOOLS = {"write_file", "str_replace"}
_ACTION_HINT_RE = re.compile(
    r"("
    r"\b(create|generate|write|edit|modify|fix|update|delete|rename|replace|refactor|implement|"
    r"inspect|check|verify|search|find|read|run|build|test|debug|analy[sz]e|summari[sz]e|review|"
    r"package|release)\b"
    r"|"
    r"(创建|生成|编写|修改|编辑|修复|更新|删除|重命名|替换|重构|实现|检查|验证|搜索|查找|读取|运行|"
    r"构建|打包|发布|测试|调试|分析|总结|审查|查看)"
    r")",
    re.IGNORECASE,
)
_ARTIFACT_HINT_RE = re.compile(
    r"("
    r"\b(file|files|folder|folders|directory|directories|workspace|project|repo|repository|code|"
    r"script|command|terminal|build|release|package|test|log|config|path|paths|prompt|doc|docs|"
    r"readme|button|ui|theme|model|tool|backend|frontend)\b"
    r"|"
    r"\.[a-z0-9]{1,8}\b"
    r"|"
    r"(文件|目录|工作区|项目|仓库|代码|脚本|命令|终端|构建|打包|发布|测试|日志|配置|路径|提示词|"
    r"文档|按钮|界面|主题|模型|工具|后端|前端)"
    r")",
    re.IGNORECASE,
)
_WRITE_REQUEST_RE = re.compile(
    r"("
    r"\b(create|generate|write|edit|modify|fix|update|delete|rename|replace|refactor|implement|patch)\b"
    r"|"
    r"(创建|生成|编写|修改|编辑|修复|更新|删除|重命名|替换|重构|实现|打补丁|改好|修好)"
    r")",
    re.IGNORECASE,
)
_NEGATED_COMPLETION_RE = re.compile(
    r"(未完成|尚未完成|还未完成|没有完成|没完成|无法完成|未能完成|"
    r"not\s+(?:done|completed|finished)|haven't\s+(?:done|completed|finished))",
    re.IGNORECASE,
)
_COMPLETION_CLAIM_RE = re.compile(
    r"(已完成|已经完成|完成了|任务完成|处理完成|搞定了|都处理好了|"
    r"\b(done|completed|finished|all set)\b)",
    re.IGNORECASE,
)
_WRITE_CLAIM_RE = re.compile(
    r"(已(?:修改|修复|创建|写入|添加|删除|更新|生成|实现)|"
    r"已经(?:修改|修复|创建|写入|添加|删除|更新|生成|实现)|"
    r"(?:改好了|修好了|写好了|创建好了|加好了|删好了)|"
    r"\b(fixed|modified|edited|created|generated|updated|wrote|written|added|deleted|removed|patched|implemented)\b)",
    re.IGNORECASE,
)
_TOOL_CLAIM_RE = re.compile(
    r"(已(?:调用|使用|运行|执行|查看|读取|搜索|检查)|"
    r"已经(?:调用|使用|运行|执行|查看|读取|搜索|检查)|"
    r"调用了(?:工具|命令|`?[a-z_]+`?)?|"
    r"使用了(?:工具|命令|`?[a-z_]+`?)?|"
    r"\b(?:i|we)\s+(?:have\s+|just\s+)?(?:called|used|ran|executed|checked|searched|read|inspected|looked at)\b)",
    re.IGNORECASE,
)
_BASH_WRITE_HINT_RE = re.compile(
    r"(apply_patch|sed\s+-i|perl\s+-0?pi?|tee\b|cat\s+>|python\s+-c|python\s+-\s*<<|node\s+-e|"
    r"touch\b|mv\b|cp\b|mkdir\b|rm\b)",
    re.IGNORECASE,
)


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
    return additional_kwargs.get("hide_from_ui") is True


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


def _parse_tool_args(raw_args: object) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _tool_call_lookup(messages: list[BaseMessage]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for message in messages:
        for tool_call in getattr(message, "tool_calls", None) or []:
            tool_call_id = tool_call.get("id")
            if isinstance(tool_call_id, str) and tool_call_id:
                lookup[tool_call_id] = tool_call
    return lookup


def _tool_message_is_error(message: ToolMessage) -> bool:
    return getattr(message, "status", None) == "error"


def _bash_tool_call_looks_like_write(tool_call: dict[str, Any]) -> bool:
    if tool_call.get("name") != "bash":
        return False
    args = _parse_tool_args(tool_call.get("args"))
    command = args.get("command") or args.get("cmd") or ""
    return isinstance(command, str) and bool(_BASH_WRITE_HINT_RE.search(command))


def _has_tool_attempt_after(messages: list[BaseMessage], start_index: int) -> bool:
    return any(isinstance(message, ToolMessage) for message in messages[start_index + 1 :])


def _has_non_error_tool_evidence_after(messages: list[BaseMessage], start_index: int) -> bool:
    return any(
        isinstance(message, ToolMessage) and not _tool_message_is_error(message)
        for message in messages[start_index + 1 :]
    )


def _has_write_evidence_after(messages: list[BaseMessage], start_index: int) -> bool:
    trailing_messages = messages[start_index + 1 :]
    lookup = _tool_call_lookup(trailing_messages)
    for message in trailing_messages:
        if not isinstance(message, ToolMessage) or _tool_message_is_error(message):
            continue
        tool_name = getattr(message, "name", None)
        if tool_name in _WRITE_EVIDENCE_TOOLS:
            return True
        if tool_name == "bash":
            matched_tool_call = lookup.get(getattr(message, "tool_call_id", ""))
            if matched_tool_call and _bash_tool_call_looks_like_write(matched_tool_call):
                return True
    return False


def _has_completion_validation_after(messages: list[BaseMessage], start_index: int) -> bool:
    return any(
        isinstance(message, HumanMessage) and getattr(message, "name", None) == COMPLETION_VALIDATION_MESSAGE_NAME
        for message in messages[start_index + 1 :]
    )


def _has_loop_control_after(messages: list[BaseMessage], start_index: int) -> bool:
    return any(_is_loop_control_message(message) for message in messages[start_index + 1 :])


def _claims_completion(text: str) -> bool:
    return not _NEGATED_COMPLETION_RE.search(text) and bool(_COMPLETION_CLAIM_RE.search(text))


def _claims_write_completion(text: str) -> bool:
    return bool(_WRITE_CLAIM_RE.search(text))


def _claims_tool_use(text: str) -> bool:
    return bool(_TOOL_CLAIM_RE.search(text))


def _has_bound_workspace(state: ThreadState) -> bool:
    thread_data = state.get("thread_data") or {}
    workspace_path = thread_data.get("workspace_path")
    workspace_container_path = thread_data.get("workspace_container_path")
    return (
        isinstance(workspace_path, str)
        and bool(workspace_path.strip())
        or isinstance(workspace_container_path, str)
        and bool(workspace_container_path.strip())
    )


def _is_observable_request(message: HumanMessage, has_bound_workspace: bool) -> bool:
    text = _message_to_text(message.content)
    if not text.strip():
        return False
    has_action_hint = bool(_ACTION_HINT_RE.search(text))
    has_artifact_hint = bool(_ARTIFACT_HINT_RE.search(text))
    return has_action_hint and (has_artifact_hint or has_bound_workspace)


def _is_write_request(message: HumanMessage, has_bound_workspace: bool) -> bool:
    text = _message_to_text(message.content)
    if not text.strip():
        return False
    has_write_hint = bool(_WRITE_REQUEST_RE.search(text))
    has_artifact_hint = bool(_ARTIFACT_HINT_RE.search(text))
    return has_write_hint and (has_artifact_hint or has_bound_workspace)


class CompletionValidationMiddleware(AgentMiddleware[ThreadState]):
    """Gently correct unsupported completion/tool-use claims."""

    state_schema = ThreadState

    def _build_warning_message(self, reason: str) -> HumanMessage:
        return HumanMessage(
            name=COMPLETION_VALIDATION_MESSAGE_NAME,
            additional_kwargs={
                "hide_from_ui": True,
                COMPLETION_VALIDATION_KEY: COMPLETION_VALIDATION_WARNING,
            },
            content=(
                "<system_reminder>\n"
                f"{reason}\n"
                "Do not claim that tools were called, files were changed, or the task is complete unless the current run contains matching tool evidence.\n"
                "If more work is needed, continue with the appropriate tool call now.\n"
                "If the task can be answered without tools, or you are blocked, say that plainly instead of implying the work already happened.\n"
                "</system_reminder>"
            ),
        )

    def _evaluate(self, state: ThreadState, runtime: Runtime) -> dict[str, list[HumanMessage]] | None:
        messages = state.get("messages") or []
        if not messages:
            return None

        last_message = messages[-1]
        if getattr(last_message, "type", None) != "ai":
            return None

        if getattr(last_message, "tool_calls", None):
            return None

        user_index = _find_last_visible_user_index(messages)
        if user_index is None:
            return None
        if _has_loop_control_after(messages, user_index):
            return None
        if _has_completion_validation_after(messages, user_index):
            return None

        user_message = messages[user_index]
        text = _message_to_text(getattr(last_message, "content", "")).strip()
        if not text:
            return None

        has_bound_workspace = _has_bound_workspace(state)
        is_observable_request = _is_observable_request(user_message, has_bound_workspace)
        is_write_request = _is_write_request(user_message, has_bound_workspace)
        has_tool_attempt = _has_tool_attempt_after(messages, user_index)
        has_tool_evidence = _has_non_error_tool_evidence_after(messages, user_index)
        has_write_evidence = _has_write_evidence_after(messages, user_index)

        reason: str | None = None
        if _claims_write_completion(text) and not has_write_evidence:
            reason = (
                "Your previous reply claimed edits or file creation, but this run does not yet contain matching write evidence."
            )
        elif _claims_tool_use(text) and not has_tool_attempt:
            reason = (
                "Your previous reply claimed tool usage, but this run does not yet contain matching tool results."
            )
        elif _claims_completion(text):
            if is_write_request and not has_write_evidence:
                reason = (
                    "Your previous reply framed the write task as complete, but this run does not yet contain matching write evidence."
                )
            elif (is_observable_request or has_tool_attempt) and not has_tool_evidence:
                reason = (
                    "Your previous reply framed the task as complete, but this run does not yet contain matching tool evidence."
                )

        if reason is None:
            return None

        thread_id = runtime.context.get("thread_id") if runtime and runtime.context else "default"
        logger.warning(
            "Completion validation reminder injected",
            extra={
                "thread_id": thread_id,
                "write_request": is_write_request,
                "observable_request": is_observable_request,
                "has_tool_attempt": has_tool_attempt,
                "has_tool_evidence": has_tool_evidence,
                "has_write_evidence": has_write_evidence,
            },
        )
        return {"messages": [self._build_warning_message(reason)]}

    @override
    def after_model(self, state: ThreadState, runtime: Runtime) -> dict[str, list[HumanMessage]] | None:
        return self._evaluate(state, runtime)

    @override
    async def aafter_model(self, state: ThreadState, runtime: Runtime) -> dict[str, list[HumanMessage]] | None:
        return self._evaluate(state, runtime)
