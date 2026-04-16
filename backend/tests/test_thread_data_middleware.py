import pytest
from langgraph.runtime import Runtime

from deerflow.agents.middlewares.thread_data_middleware import ThreadDataMiddleware


def _as_posix(path: str) -> str:
    return path.replace("\\", "/")


class TestThreadDataMiddleware:
    def test_before_agent_returns_paths_when_thread_id_present_in_context(self, tmp_path):
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)

        result = middleware.before_agent(state={}, runtime=Runtime(context={"thread_id": "thread-123"}))

        assert result is not None
        assert _as_posix(result["thread_data"]["workspace_path"]).endswith("threads/thread-123/user-data/workspace")
        assert _as_posix(result["thread_data"]["uploads_path"]).endswith("threads/thread-123/user-data/uploads")
        assert _as_posix(result["thread_data"]["outputs_path"]).endswith("threads/thread-123/user-data/outputs")

    def test_before_agent_uses_thread_id_from_configurable_when_context_is_none(self, tmp_path, monkeypatch):
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)
        runtime = Runtime(context=None)
        monkeypatch.setattr(
            "deerflow.agents.middlewares.thread_data_middleware.get_config",
            lambda: {"configurable": {"thread_id": "thread-from-config"}},
        )

        result = middleware.before_agent(state={}, runtime=runtime)

        assert result is not None
        assert _as_posix(result["thread_data"]["workspace_path"]).endswith("threads/thread-from-config/user-data/workspace")
        assert runtime.context is None

    def test_before_agent_uses_thread_id_from_configurable_when_context_missing_thread_id(self, tmp_path, monkeypatch):
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)
        runtime = Runtime(context={})
        monkeypatch.setattr(
            "deerflow.agents.middlewares.thread_data_middleware.get_config",
            lambda: {"configurable": {"thread_id": "thread-from-config"}},
        )

        result = middleware.before_agent(state={}, runtime=runtime)

        assert result is not None
        assert _as_posix(result["thread_data"]["uploads_path"]).endswith("threads/thread-from-config/user-data/uploads")
        assert runtime.context == {}

    def test_before_agent_preserves_bound_workspace_container_path(self, tmp_path):
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)

        result = middleware.before_agent(
            state={},
            runtime=Runtime(
                context={
                    "thread_id": "thread-123",
                    "workspace_path": "/Users/demo/project",
                    "workspace_container_path": "/mnt/projects/demo",
                }
            ),
        )

        assert result is not None
        assert result["thread_data"]["workspace_path"] == "/Users/demo/project"
        assert result["thread_data"]["workspace_container_path"] == "/mnt/projects/demo"

    def test_before_agent_falls_back_to_configurable_workspace_when_runtime_context_omits_it(self, tmp_path, monkeypatch):
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)
        monkeypatch.setattr(
            "deerflow.agents.middlewares.thread_data_middleware.get_config",
            lambda: {
                "configurable": {
                    "thread_id": "thread-from-config",
                    "workspace_path": "/Users/demo/project",
                    "workspace_container_path": "/mnt/projects/demo",
                }
            },
        )

        result = middleware.before_agent(
            state={},
            runtime=Runtime(context={}),
        )

        assert result is not None
        assert result["thread_data"]["workspace_path"] == "/Users/demo/project"
        assert result["thread_data"]["workspace_container_path"] == "/mnt/projects/demo"

    def test_before_agent_preserves_inherited_thread_data_from_parent_agent(self, tmp_path):
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)

        result = middleware.before_agent(
            state={
                "thread_data": {
                    "workspace_path": "/Users/pippo/Downloads/deerflow",
                    "workspace_container_path": "/mnt/workspaces/downloads/deerflow",
                    "uploads_path": "/mnt/workspaces/downloads/deerflow/.thread/uploads",
                    "outputs_path": "/mnt/workspaces/downloads/deerflow/.thread/outputs",
                }
            },
            runtime=Runtime(context={"thread_id": "thread-123"}),
        )

        assert result is not None
        assert result["thread_data"]["workspace_path"] == "/Users/pippo/Downloads/deerflow"
        assert result["thread_data"]["workspace_container_path"] == "/mnt/workspaces/downloads/deerflow"
        assert result["thread_data"]["uploads_path"] == "/mnt/workspaces/downloads/deerflow/.thread/uploads"
        assert result["thread_data"]["outputs_path"] == "/mnt/workspaces/downloads/deerflow/.thread/outputs"

    def test_before_agent_prefers_explicit_runtime_workspace_over_inherited_parent_state(self, tmp_path):
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)

        result = middleware.before_agent(
            state={
                "thread_data": {
                    "workspace_path": "/Users/old/project",
                    "workspace_container_path": "/mnt/old/project",
                }
            },
            runtime=Runtime(
                context={
                    "thread_id": "thread-123",
                    "workspace_path": "/Users/new/project",
                    "workspace_container_path": "/mnt/new/project",
                }
            ),
        )

        assert result is not None
        assert result["thread_data"]["workspace_path"] == "/Users/new/project"
        assert result["thread_data"]["workspace_container_path"] == "/mnt/new/project"

    def test_before_agent_raises_clear_error_when_thread_id_missing_everywhere(self, tmp_path, monkeypatch):
        middleware = ThreadDataMiddleware(base_dir=str(tmp_path), lazy_init=True)
        monkeypatch.setattr(
            "deerflow.agents.middlewares.thread_data_middleware.get_config",
            lambda: {"configurable": {}},
        )

        with pytest.raises(ValueError, match="Thread ID is required in runtime context or config.configurable"):
            middleware.before_agent(state={}, runtime=Runtime(context=None))
