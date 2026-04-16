"""Tests for TodoMiddleware context-loss detection."""

import asyncio
from unittest.mock import MagicMock

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.middlewares.todo_middleware import (
    TodoMiddleware,
    _format_todos,
    _is_complex_request,
    _reminder_in_messages,
    _todos_in_messages,
)


def _ai_with_write_todos():
    return AIMessage(content="", tool_calls=[{"name": "write_todos", "id": "tc_1", "args": {}}])


def _reminder_msg():
    return HumanMessage(name="todo_reminder", content="reminder")


def _make_runtime():
    runtime = MagicMock()
    runtime.context = {"thread_id": "test-thread"}
    return runtime


def _sample_todos():
    return [
        {"status": "completed", "content": "Set up project"},
        {"status": "in_progress", "content": "Write tests"},
        {"status": "pending", "content": "Deploy"},
    ]


class TestTodosInMessages:
    def test_true_when_write_todos_present(self):
        msgs = [HumanMessage(content="hi"), _ai_with_write_todos()]
        assert _todos_in_messages(msgs) is True

    def test_false_when_no_write_todos(self):
        msgs = [
            HumanMessage(content="hi"),
            AIMessage(content="hello", tool_calls=[{"name": "bash", "id": "tc_1", "args": {}}]),
        ]
        assert _todos_in_messages(msgs) is False

    def test_false_for_empty_list(self):
        assert _todos_in_messages([]) is False

    def test_false_for_ai_without_tool_calls(self):
        msgs = [AIMessage(content="hello")]
        assert _todos_in_messages(msgs) is False


class TestReminderInMessages:
    def test_true_when_reminder_present(self):
        msgs = [HumanMessage(content="hi"), _reminder_msg()]
        assert _reminder_in_messages(msgs) is True

    def test_false_when_no_reminder(self):
        msgs = [HumanMessage(content="hi"), AIMessage(content="hello")]
        assert _reminder_in_messages(msgs) is False

    def test_false_for_empty_list(self):
        assert _reminder_in_messages([]) is False

    def test_false_for_human_without_name(self):
        msgs = [HumanMessage(content="todo_reminder")]
        assert _reminder_in_messages(msgs) is False


class TestFormatTodos:
    def test_formats_multiple_items(self):
        todos = _sample_todos()
        result = _format_todos(todos)
        assert "- [completed] Set up project" in result
        assert "- [in_progress] Write tests" in result
        assert "- [pending] Deploy" in result

    def test_empty_list(self):
        assert _format_todos([]) == ""

    def test_missing_fields_use_defaults(self):
        todos = [{"content": "No status"}, {"status": "done"}]
        result = _format_todos(todos)
        assert "- [pending] No status" in result
        assert "- [done] " in result


class TestBeforeModel:
    def test_returns_none_when_no_todos(self):
        mw = TodoMiddleware()
        state = {"messages": [HumanMessage(content="hi")], "todos": []}
        assert mw.before_model(state, _make_runtime()) is None

    def test_bootstrap_reminder_for_complex_request_without_todos(self):
        mw = TodoMiddleware()
        state = {
            "messages": [HumanMessage(content="请按步骤修改三个文件，然后回读确认并总结")],
            "todos": [],
        }
        result = mw.before_model(state, _make_runtime())
        assert result is not None
        assert result["messages"][0].name == "todo_bootstrap_reminder"

    def test_no_bootstrap_for_simple_read_only_request(self):
        mw = TodoMiddleware()
        state = {
            "messages": [HumanMessage(content="看下这个目录有哪些文件")],
            "todos": [],
        }
        assert mw.before_model(state, _make_runtime()) is None

    def test_returns_none_when_todos_is_none(self):
        mw = TodoMiddleware()
        state = {"messages": [HumanMessage(content="hi")], "todos": None}
        assert mw.before_model(state, _make_runtime()) is None

    def test_returns_none_when_write_todos_still_visible(self):
        mw = TodoMiddleware()
        state = {
            "messages": [_ai_with_write_todos()],
            "todos": _sample_todos(),
        }
        assert mw.before_model(state, _make_runtime()) is None

    def test_returns_none_when_reminder_already_present(self):
        mw = TodoMiddleware()
        state = {
            "messages": [HumanMessage(content="hi"), _reminder_msg()],
            "todos": _sample_todos(),
        }
        assert mw.before_model(state, _make_runtime()) is None

    def test_injects_reminder_when_todos_exist_but_truncated(self):
        mw = TodoMiddleware()
        state = {
            "messages": [HumanMessage(content="hi"), AIMessage(content="sure")],
            "todos": _sample_todos(),
        }
        result = mw.before_model(state, _make_runtime())
        assert result is not None
        msgs = result["messages"]
        assert len(msgs) == 1
        assert isinstance(msgs[0], HumanMessage)
        assert msgs[0].name == "todo_reminder"

    def test_reminder_contains_formatted_todos(self):
        mw = TodoMiddleware()
        state = {
            "messages": [HumanMessage(content="hi")],
            "todos": _sample_todos(),
        }
        result = mw.before_model(state, _make_runtime())
        content = result["messages"][0].content
        assert "Set up project" in content
        assert "Write tests" in content
        assert "Deploy" in content
        assert "system_reminder" in content


class TestAbeforeModel:
    def test_delegates_to_sync(self):
        mw = TodoMiddleware()
        state = {
            "messages": [HumanMessage(content="hi")],
            "todos": _sample_todos(),
        }
        result = asyncio.run(mw.abefore_model(state, _make_runtime()))
        assert result is not None
        assert result["messages"][0].name == "todo_reminder"


class TestWrapModelCall:
    def test_force_required_tool_choice_for_complex_task_without_todos(self):
        mw = TodoMiddleware()
        captured = {}

        request = ModelRequest(
            model=MagicMock(),
            system_prompt=None,
            messages=[HumanMessage(content="1. 修改代码\n2. 更新测试\n3. 回读确认")],
            tool_choice=None,
            tools=[{"type": "function", "function": {"name": "write_todos"}}],
            state={"messages": [HumanMessage(content="1. 修改代码\n2. 更新测试\n3. 回读确认")], "todos": []},
            runtime=MagicMock(),
            model_settings={},
        )

        def _handler(req: ModelRequest):
            captured["tool_choice"] = req.tool_choice
            return AIMessage(content="")

        mw.wrap_model_call(request, _handler)
        assert captured["tool_choice"] == "required"

    def test_do_not_force_when_todos_exist(self):
        mw = TodoMiddleware()
        captured = {}

        request = ModelRequest(
            model=MagicMock(),
            system_prompt=None,
            messages=[HumanMessage(content="请继续")],
            tool_choice=None,
            tools=[{"type": "function", "function": {"name": "write_todos"}}],
            state={"messages": [HumanMessage(content="请继续")], "todos": _sample_todos()},
            runtime=MagicMock(),
            model_settings={},
        )

        def _handler(req: ModelRequest):
            captured["tool_choice"] = req.tool_choice
            return AIMessage(content="")

        mw.wrap_model_call(request, _handler)
        assert captured["tool_choice"] is None


class TestComplexityHeuristic:
    def test_complex_for_multi_file_edit_and_verify(self):
        msg = HumanMessage(
            content=(
                "在这个工作区里，搜索 calculate_total 的实现和调用点。"
                "修改 app/calc.py、app/report.py 和 tests/test_calc.py，"
                "最后回读关键文件并总结。"
            )
        )
        assert _is_complex_request(msg) is True

    def test_not_complex_for_simple_listing(self):
        msg = HumanMessage(content="列出当前目录文件")
        assert _is_complex_request(msg) is False
