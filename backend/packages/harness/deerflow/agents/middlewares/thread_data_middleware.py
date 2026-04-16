import logging
from typing import NotRequired, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.config import get_config
from langgraph.runtime import Runtime

from deerflow.agents.thread_state import ThreadDataState
from deerflow.config.paths import Paths, get_paths

logger = logging.getLogger(__name__)


class ThreadDataMiddlewareState(AgentState):
    """Compatible with the `ThreadState` schema."""

    thread_data: NotRequired[ThreadDataState | None]


class ThreadDataMiddleware(AgentMiddleware[ThreadDataMiddlewareState]):
    """Create thread data directories for each thread execution.

    Creates the following directory structure:
    - {base_dir}/threads/{thread_id}/user-data/workspace
    - {base_dir}/threads/{thread_id}/user-data/uploads
    - {base_dir}/threads/{thread_id}/user-data/outputs

    Lifecycle Management:
    - With lazy_init=True (default): Only compute paths, directories created on-demand
    - With lazy_init=False: Eagerly create directories in before_agent()
    """

    state_schema = ThreadDataMiddlewareState

    def __init__(self, base_dir: str | None = None, lazy_init: bool = True):
        """Initialize the middleware.

        Args:
            base_dir: Base directory for thread data. Defaults to Paths resolution.
            lazy_init: If True, defer directory creation until needed.
                      If False, create directories eagerly in before_agent().
                      Default is True for optimal performance.
        """
        super().__init__()
        self._paths = Paths(base_dir) if base_dir else get_paths()
        self._lazy_init = lazy_init

    def _get_thread_paths(self, thread_id: str) -> dict[str, str]:
        """Get the paths for a thread's data directories.

        Args:
            thread_id: The thread ID.

        Returns:
            Dictionary with workspace_path, uploads_path, and outputs_path.
        """
        return {
            "workspace_path": str(self._paths.sandbox_work_dir(thread_id)),
            "uploads_path": str(self._paths.sandbox_uploads_dir(thread_id)),
            "outputs_path": str(self._paths.sandbox_outputs_dir(thread_id)),
        }

    def _create_thread_directories(self, thread_id: str) -> dict[str, str]:
        """Create the thread data directories.

        Args:
            thread_id: The thread ID.

        Returns:
            Dictionary with the created directory paths.
        """
        self._paths.ensure_thread_dirs(thread_id)
        return self._get_thread_paths(thread_id)

    def _merge_thread_data(
        self,
        default_paths: dict[str, str],
        existing_thread_data: ThreadDataState | None,
        explicit_overrides: dict[str, str],
    ) -> dict[str, str]:
        """Merge thread data sources while preserving inherited workspace bindings.

        Precedence:
        1. computed thread-local defaults
        2. existing state inherited from the parent agent
        3. explicit runtime/config overrides for this execution
        """
        merged = dict(default_paths)

        if existing_thread_data:
            for key, value in existing_thread_data.items():
                if isinstance(value, str) and value.strip():
                    merged[key] = value

        for key, value in explicit_overrides.items():
            if isinstance(value, str) and value.strip():
                merged[key] = value

        return merged

    @override
    def before_agent(self, state: ThreadDataMiddlewareState, runtime: Runtime) -> dict | None:
        context = runtime.context or {}
        thread_id = context.get("thread_id")
        configurable: dict[str, object] = {}
        if thread_id is None:
            config = get_config()
            configurable = config.get("configurable", {})
            thread_id = configurable.get("thread_id")
        elif not configurable:
            try:
                configurable = get_config().get("configurable", {})
            except Exception:
                configurable = {}

        if thread_id is None:
            raise ValueError("Thread ID is required in runtime context or config.configurable")

        if self._lazy_init:
            # Lazy initialization: only compute paths, don't create directories
            paths = self._get_thread_paths(thread_id)
        else:
            # Eager initialization: create directories immediately
            paths = self._create_thread_directories(thread_id)
            logger.debug("Created thread data directories for thread %s", thread_id)

        explicit_overrides: dict[str, str] = {}
        for key in ("workspace_path", "workspace_container_path", "uploads_path", "outputs_path"):
            selected_value = context.get(key)
            if not isinstance(selected_value, str) or not selected_value.strip():
                selected_value = configurable.get(key)
            if isinstance(selected_value, str) and selected_value.strip():
                explicit_overrides[key] = selected_value

        paths = self._merge_thread_data(
            default_paths=paths,
            existing_thread_data=state.get("thread_data"),
            explicit_overrides=explicit_overrides,
        )

        return {
            "thread_data": {
                **paths,
            }
        }
