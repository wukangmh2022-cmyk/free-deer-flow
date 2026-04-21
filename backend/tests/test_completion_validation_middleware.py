from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from deerflow.agents.middlewares.completion_validation_middleware import (
    COMPLETION_VALIDATION_KEY,
    COMPLETION_VALIDATION_MESSAGE_NAME,
    COMPLETION_VALIDATION_WARNING,
    CompletionValidationMiddleware,
)


def _runtime(thread_id: str = "thread-1"):
    runtime = MagicMock()
    runtime.context = {"thread_id": thread_id}
    return runtime


def _state(messages, workspace: bool = False):
    state = {"messages": messages}
    if workspace:
        state["thread_data"] = {
            "workspace_path": "/Users/demo/project",
            "workspace_container_path": "/mnt/project",
        }
    return state


def _write_call(tool_name: str = "write_file", tool_call_id: str = "call_1"):
    return {"name": tool_name, "id": tool_call_id, "args": {"path": "/mnt/project/app.py", "content": "demo"}}


def test_after_model_skips_plain_text_without_claim():
    middleware = CompletionValidationMiddleware()
    state = _state(
        [
            HumanMessage(content="请帮我想三个更简洁的按钮文案"),
            AIMessage(content="这里有三个可选文案。"),
        ]
    )

    assert middleware.after_model(state, _runtime()) is None


def test_after_model_injects_warning_for_write_claim_without_write_evidence():
    middleware = CompletionValidationMiddleware()
    state = _state(
        [
            HumanMessage(content="请修复 workspace 里的按钮溢出问题"),
            AIMessage(
                content="先查看一下文件",
                tool_calls=[{"name": "read_file", "id": "call_1", "args": {"path": "/mnt/project/app.tsx"}}],
            ),
            ToolMessage(content="button code", tool_call_id="call_1", name="read_file"),
            AIMessage(content="已完成修改，图标大小已经调好了。"),
        ],
        workspace=True,
    )

    result = middleware.after_model(state, _runtime())

    assert result is not None
    reminder = result["messages"][0]
    assert reminder.name == COMPLETION_VALIDATION_MESSAGE_NAME
    assert reminder.additional_kwargs["hide_from_ui"] is True
    assert reminder.additional_kwargs[COMPLETION_VALIDATION_KEY] == COMPLETION_VALIDATION_WARNING
    assert "write evidence" in str(reminder.content)


def test_after_model_skips_when_current_ai_message_contains_tool_calls():
    middleware = CompletionValidationMiddleware()
    state = _state(
        [
            HumanMessage(content="请修复 workspace 里的按钮溢出问题"),
            AIMessage(content="我去改", tool_calls=[_write_call()]),
        ],
        workspace=True,
    )

    assert middleware.after_model(state, _runtime()) is None


def test_after_model_skips_when_matching_write_evidence_exists():
    middleware = CompletionValidationMiddleware()
    state = _state(
        [
            HumanMessage(content="请修复 workspace 里的按钮溢出问题"),
            AIMessage(content="开始修改", tool_calls=[_write_call()]),
            ToolMessage(content="ok", tool_call_id="call_1", name="write_file"),
            AIMessage(content="已完成修改，图标大小已经调好了。"),
        ],
        workspace=True,
    )

    assert middleware.after_model(state, _runtime()) is None


def test_after_model_skips_analysis_only_completion_without_observable_context():
    middleware = CompletionValidationMiddleware()
    state = _state(
        [
            HumanMessage(content="给我一份 React 性能优化 checklist"),
            AIMessage(content="已完成整理，下面给你一个清单。"),
        ]
    )

    assert middleware.after_model(state, _runtime()) is None


def test_after_model_injects_warning_for_tool_claim_without_tool_results():
    middleware = CompletionValidationMiddleware()
    state = _state(
        [
            HumanMessage(content="请检查这个文件的配置有没有问题"),
            AIMessage(content="我已经调用了 `read_file` 检查过这个文件，没有问题。"),
        ],
        workspace=True,
    )

    result = middleware.after_model(state, _runtime())

    assert result is not None
    reminder = result["messages"][0]
    assert "claimed tool usage" in str(reminder.content)


def test_after_model_does_not_repeat_warning_in_same_turn():
    middleware = CompletionValidationMiddleware()
    state = _state(
        [
            HumanMessage(content="请修复 workspace 里的按钮溢出问题"),
            AIMessage(content="已完成修改。"),
            HumanMessage(
                name=COMPLETION_VALIDATION_MESSAGE_NAME,
                content="hidden warning",
                additional_kwargs={
                    "hide_from_ui": True,
                    COMPLETION_VALIDATION_KEY: COMPLETION_VALIDATION_WARNING,
                },
            ),
            AIMessage(content="还是已完成修改。"),
        ],
        workspace=True,
    )

    assert middleware.after_model(state, _runtime()) is None


@pytest.mark.anyio
async def test_aafter_model_matches_sync_behavior():
    middleware = CompletionValidationMiddleware()
    state = _state(
        [
            HumanMessage(content="请修复 workspace 里的按钮溢出问题"),
            AIMessage(content="已完成修改。"),
        ],
        workspace=True,
    )

    result = await middleware.aafter_model(state, _runtime())

    assert result is not None
    assert result["messages"][0].name == COMPLETION_VALIDATION_MESSAGE_NAME
