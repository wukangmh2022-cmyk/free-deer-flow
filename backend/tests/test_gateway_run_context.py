from types import SimpleNamespace

import pytest

from app.gateway.routers.thread_runs import RunCreateRequest
from app.gateway.services import start_run
from deerflow.runtime import MemoryStreamBridge, RunManager


class _CheckpointerStub:
    async def aget_tuple(self, _config):
        return None


@pytest.mark.anyio
async def test_start_run_preserves_workspace_context(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_run_agent(*args, **kwargs):
        captured["config"] = kwargs["config"]

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                stream_bridge=MemoryStreamBridge(),
                run_manager=RunManager(),
                checkpointer=_CheckpointerStub(),
                store=None,
            )
        )
    )

    body = RunCreateRequest(
        input={"messages": [{"role": "user", "content": "看下目录有哪些文件"}]},
        context={
            "workspace_path": "/Users/pippo/Downloads/deerflow",
            "workspace_container_path": "/mnt/workspaces/downloads/deerflow",
            "model_name": "deepseek-web-deerflow",
            "thread_id": "wrong-thread-id",
        },
    )

    monkeypatch.setattr("app.gateway.services.run_agent", fake_run_agent)
    monkeypatch.setattr("app.gateway.services.resolve_agent_factory", lambda _assistant_id: object())

    record = await start_run(body, "thread-123", request)
    assert record.task is not None
    await record.task

    assert captured["config"]["context"] == {
        "workspace_path": "/Users/pippo/Downloads/deerflow",
        "workspace_container_path": "/mnt/workspaces/downloads/deerflow",
        "model_name": "deepseek-web-deerflow",
        "thread_id": "thread-123",
    }
    assert captured["config"]["configurable"]["model_name"] == "deepseek-web-deerflow"
