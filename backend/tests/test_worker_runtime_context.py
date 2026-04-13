import pytest

from deerflow.runtime import MemoryStreamBridge, RunManager, run_agent


class _CheckpointerStub:
    async def aget_tuple(self, _config):
        return None


@pytest.mark.anyio
async def test_run_agent_preserves_context_in_runtime(monkeypatch):
    captured: dict[str, object] = {}

    class FakeRuntime:
        def __init__(self, *, context=None, store=None):
            self.context = context
            self.store = store

    class FakeRunnableConfig(dict):
        pass

    class DummyAgent:
        async def astream(self, graph_input, config, stream_mode=None):
            captured["graph_input"] = graph_input
            captured["config"] = config
            captured["stream_mode"] = stream_mode
            if False:
                yield None

    def agent_factory(*, config):
        captured["factory_config"] = config
        return DummyAgent()

    def discard_task(coro):
        coro.close()
        return None

    monkeypatch.setattr("langgraph.runtime.Runtime", FakeRuntime)
    monkeypatch.setattr("langchain_core.runnables.RunnableConfig", FakeRunnableConfig)
    monkeypatch.setattr("deerflow.runtime.runs.worker.asyncio.create_task", discard_task)

    run_manager = RunManager()
    record = await run_manager.create("thread-123")
    bridge = MemoryStreamBridge()
    config = {
        "context": {
            "workspace_path": "/Users/pippo/Downloads/deerflow",
            "workspace_container_path": "/mnt/workspaces/downloads/deerflow",
            "model_name": "deepseek-web-deerflow",
        }
    }

    await run_agent(
        bridge,
        run_manager,
        record,
        checkpointer=_CheckpointerStub(),
        store=None,
        agent_factory=agent_factory,
        graph_input={"messages": []},
        config=config,
    )

    runtime = captured["config"]["configurable"]["__pregel_runtime"]
    expected_context = {
        "workspace_path": "/Users/pippo/Downloads/deerflow",
        "workspace_container_path": "/mnt/workspaces/downloads/deerflow",
        "model_name": "deepseek-web-deerflow",
        "thread_id": "thread-123",
    }

    assert isinstance(runtime, FakeRuntime)
    assert runtime.context == expected_context
    assert config["context"] == expected_context
    assert captured["factory_config"]["configurable"]["__pregel_runtime"] is runtime
