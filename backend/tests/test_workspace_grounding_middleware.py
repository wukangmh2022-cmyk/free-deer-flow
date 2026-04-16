from types import SimpleNamespace

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import HumanMessage, ToolMessage

from deerflow.agents.middlewares.loop_detection_middleware import (
    LOOP_CONTROL_KEY,
    LOOP_CONTROL_MESSAGE_NAME,
    LOOP_CONTROL_WARNING,
)
from deerflow.agents.middlewares.workspace_grounding_middleware import (
    WorkspaceGroundingMiddleware,
)


def _state(messages):
    return {
        "messages": messages,
        "thread_data": {
            "workspace_path": "/Users/demo/project",
            "workspace_container_path": "/mnt/project",
        },
    }


def test_before_model_injects_explore_message_for_workspace_request():
    middleware = WorkspaceGroundingMiddleware()
    state = _state([HumanMessage(content="看看有哪些py文件，并不输出代码只输出你阅读后的理解")])

    result = middleware.before_model(state, runtime=None)

    assert result is not None
    reminder = result["messages"][0]
    assert reminder.name == "workspace_grounding"
    assert reminder.additional_kwargs["hide_from_ui"] is True
    assert "Inspect the bound workspace first" in str(reminder.content)


def test_before_model_skips_non_workspace_requests():
    middleware = WorkspaceGroundingMiddleware()
    state = _state([HumanMessage(content="你好，介绍一下你自己")])

    assert middleware.before_model(state, runtime=None) is None


def test_before_model_does_not_duplicate_initial_grounding_message():
    middleware = WorkspaceGroundingMiddleware()
    state = _state(
        [
            HumanMessage(content="分析源码"),
            HumanMessage(
                name="workspace_grounding",
                additional_kwargs={"hide_from_ui": True},
                content="hidden reminder",
            ),
        ]
    )

    assert middleware.before_model(state, runtime=None) is None


def test_before_model_injects_grounded_answer_message_after_tool_result():
    middleware = WorkspaceGroundingMiddleware()
    state = _state(
        [
            HumanMessage(content="Find all CSV files in the project"),
            ToolMessage(
                content="/mnt/project/a.csv\n/mnt/project/b.csv",
                tool_call_id="call_1",
                name="ls",
            ),
        ]
    )

    result = middleware.before_model(state, runtime=None)

    assert result is not None
    reminder = result["messages"][0]
    assert reminder.name == "workspace_grounding"
    assert "authoritative" in str(reminder.content)
    assert "Quote exact filenames" in str(reminder.content)


def test_before_model_requests_read_file_after_discovery_for_deep_request():
    middleware = WorkspaceGroundingMiddleware()
    state = _state(
        [
            HumanMessage(content="分析并总结这个项目的 context compression 实现"),
            ToolMessage(
                content="/mnt/project/app.py:10: summarization enabled",
                tool_call_id="call_1",
                name="grep",
            ),
        ]
    )

    result = middleware.before_model(state, runtime=None)

    assert result is not None
    reminder = result["messages"][0]
    assert "only have discovery/search results" in str(reminder.content)
    assert "`read_file`" in str(reminder.content)


def test_before_model_skips_grounding_after_loop_control_warning():
    middleware = WorkspaceGroundingMiddleware()
    state = _state(
        [
            HumanMessage(content="分析并总结这个项目的 context compression 实现"),
            ToolMessage(
                content="/mnt/project/app.py:10: summarization enabled",
                tool_call_id="call_1",
                name="grep",
            ),
            HumanMessage(
                name=LOOP_CONTROL_MESSAGE_NAME,
                additional_kwargs={
                    "hide_from_ui": True,
                    LOOP_CONTROL_KEY: LOOP_CONTROL_WARNING,
                },
                content="[LOOP DETECTED] stop calling tools",
            ),
        ]
    )

    assert middleware.before_model(state, runtime=None) is None


def test_wrap_model_call_requires_tool_choice_for_workspace_evidence_request():
    middleware = WorkspaceGroundingMiddleware()
    request = ModelRequest(
        model=object(),
        messages=[HumanMessage(content="Find all Excel or CSV overtime files")],
        tools=[SimpleNamespace(name="ls"), SimpleNamespace(name="read_file")],
        state=_state([HumanMessage(content="Find all Excel or CSV overtime files")]),
        runtime=None,
    )
    seen = {}

    def handler(req):
        seen["tool_choice"] = req.tool_choice
        return req.tool_choice

    result = middleware.wrap_model_call(request, handler)

    assert result == "required"
    assert seen["tool_choice"] == "required"


def test_wrap_model_call_requires_another_tool_for_deep_request_after_grep():
    middleware = WorkspaceGroundingMiddleware()
    messages = [
        HumanMessage(content="请分析并总结 context compression 实现"),
        ToolMessage(
            content="/mnt/project/app.py:10: summarization enabled",
            tool_call_id="call_1",
            name="grep",
        ),
    ]
    request = ModelRequest(
        model=object(),
        messages=messages,
        tool_choice="auto",
        tools=[SimpleNamespace(name="grep"), SimpleNamespace(name="read_file")],
        state=_state(messages),
        runtime=None,
    )
    seen = {}

    def handler(req):
        seen["tool_choice"] = req.tool_choice
        return req.tool_choice

    result = middleware.wrap_model_call(request, handler)

    assert result == "required"
    assert seen["tool_choice"] == "required"


def test_wrap_model_call_keeps_existing_tool_choice_after_evidence_tool():
    middleware = WorkspaceGroundingMiddleware()
    messages = [
        HumanMessage(content="Find all Excel or CSV overtime files"),
        ToolMessage(
            content="/mnt/project/data.csv",
            tool_call_id="call_1",
            name="ls",
        ),
    ]
    request = ModelRequest(
        model=object(),
        messages=messages,
        tool_choice="auto",
        tools=[SimpleNamespace(name="ls"), SimpleNamespace(name="read_file")],
        state=_state(messages),
        runtime=None,
    )
    seen = {}

    def handler(req):
        seen["tool_choice"] = req.tool_choice
        return req.tool_choice

    result = middleware.wrap_model_call(request, handler)

    assert result == "auto"
    assert seen["tool_choice"] == "auto"


def test_wrap_model_call_does_not_force_tools_after_loop_control_warning():
    middleware = WorkspaceGroundingMiddleware()
    messages = [
        HumanMessage(content="请分析并总结 context compression 实现"),
        ToolMessage(
            content="/mnt/project/app.py:10: summarization enabled",
            tool_call_id="call_1",
            name="grep",
        ),
        HumanMessage(
            name=LOOP_CONTROL_MESSAGE_NAME,
            additional_kwargs={
                "hide_from_ui": True,
                LOOP_CONTROL_KEY: LOOP_CONTROL_WARNING,
            },
            content="[LOOP DETECTED] stop calling tools",
        ),
    ]
    request = ModelRequest(
        model=object(),
        messages=messages,
        tool_choice="auto",
        tools=[SimpleNamespace(name="grep"), SimpleNamespace(name="read_file")],
        state=_state(messages),
        runtime=None,
    )
    seen = {}

    def handler(req):
        seen["tool_choice"] = req.tool_choice
        return req.tool_choice

    result = middleware.wrap_model_call(request, handler)

    assert result == "auto"
    assert seen["tool_choice"] == "auto"


def test_before_model_injects_write_reminder_when_edit_requested_but_no_write_tool_used():
    middleware = WorkspaceGroundingMiddleware()
    state = _state(
        [
            HumanMessage(content="请修改 app/calc.py，把参数名改为 adjustmentnew，并同步修改测试"),
            ToolMessage(
                content="from app.config import SERVICE_FEE\n\ndef calculate_total(subtotal: int, adjustment: int = 0) -> int:\n    return subtotal + adjustment + SERVICE_FEE\n",
                tool_call_id="call_1",
                name="read_file",
            ),
        ]
    )

    result = middleware.before_model(state, runtime=None)

    assert result is not None
    reminder = result["messages"][0]
    assert "real file modifications" in str(reminder.content)
    assert "Do not claim edits are done" in str(reminder.content)


def test_wrap_model_call_requires_write_tool_for_edit_requests_after_read_only_evidence():
    middleware = WorkspaceGroundingMiddleware()
    messages = [
        HumanMessage(content="修改 app/calc.py 并同步 app/report.py 和 tests/test_calc.py"),
        ToolMessage(
            content="def calculate_total(subtotal: int, adjustment: int = 0) -> int: ...",
            tool_call_id="call_1",
            name="read_file",
        ),
    ]
    request = ModelRequest(
        model=object(),
        messages=messages,
        tool_choice="auto",
        tools=[SimpleNamespace(name="read_file"), SimpleNamespace(name="write_file"), SimpleNamespace(name="str_replace")],
        state=_state(messages),
        runtime=None,
    )
    seen = {}

    def handler(req):
        seen["tool_choice"] = req.tool_choice
        return req.tool_choice

    result = middleware.wrap_model_call(request, handler)

    assert result == "required"
    assert seen["tool_choice"] == "required"
