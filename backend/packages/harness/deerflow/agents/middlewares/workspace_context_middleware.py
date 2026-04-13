"""Middleware for injecting the current thread workspace into model context."""

from __future__ import annotations

from typing import Any, override

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from deerflow.agents.thread_state import ThreadState

_WORKSPACE_MESSAGE_NAME = "workspace_context"


class WorkspaceContextMiddleware(AgentMiddleware[ThreadState]):
    """Inject a one-time reminder describing the bound workspace for this thread."""

    state_schema = ThreadState

    def _has_workspace_message(self, messages: list[Any]) -> bool:
        for msg in messages:
            if isinstance(msg, HumanMessage) and getattr(msg, "name", None) == _WORKSPACE_MESSAGE_NAME:
                return True
        return False

    def _build_workspace_message(
        self,
        workspace_path: str,
        workspace_tool_path: str | None,
        uploads_path: str | None,
        outputs_path: str | None,
    ) -> HumanMessage:
        preferred_workspace_tool_path = workspace_tool_path or "/mnt/user-data/workspace"
        uploads_tool_path = "/mnt/user-data/uploads"
        outputs_tool_path = "/mnt/user-data/outputs"
        return HumanMessage(
            name=_WORKSPACE_MESSAGE_NAME,
            additional_kwargs={"hide_from_ui": True},
            content=(
                "<system_reminder>\n"
                "This thread already has a bound working directory.\n"
                f"- User-facing workspace path: `{workspace_path}`\n"
                f"- Preferred workspace tool path: `{preferred_workspace_tool_path}`\n"
                f"- Attachment tool path: `{uploads_tool_path}`\n"
                f"- Output tool path: `{outputs_tool_path}`\n\n"
                "Always use the preferred workspace tool path in tool calls when you need to inspect or edit the bound workspace.\n"
                "When the user's request plausibly refers to materials already available in this thread — files, directories, documents, datasets, code, outputs, or project contents — "
                "you should treat the bound workspace as the default target and inspect it directly instead of asking which path to use.\n"
                "If the user says things like 'this pdf', 'this file', 'the code here', or refers to the current project without naming a path, start with the bound workspace.\n"
                "Treat uploads as a secondary location for explicit message attachments or previously imported thread files, not the default place to begin.\n"
                "For documents that already live inside the bound workspace or a mounted project directory, use their workspace path directly; supported PDFs and Office files can be read with `read_file` without asking the user to upload them again.\n"
                "Spend a small exploration budget on cheap local evidence first: inspect the workspace, uploads, outputs, and recent artifacts before asking for more details.\n"
                "Only ask the user for a file path or target after you have checked the current workspace and still cannot identify the relevant material.\n"
                "Only ask for another path if the user explicitly wants a different directory.\n"
                "</system_reminder>"
            ),
        )

    def _inject_workspace_message(self, state: ThreadState) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        if self._has_workspace_message(messages):
            return None

        thread_data = state.get("thread_data") or {}
        workspace_path = thread_data.get("workspace_path")
        if not isinstance(workspace_path, str) or not workspace_path.strip():
            return None

        workspace_tool_path = thread_data.get("workspace_container_path")
        uploads_path = thread_data.get("uploads_path")
        outputs_path = thread_data.get("outputs_path")
        reminder = self._build_workspace_message(
            workspace_path,
            workspace_tool_path,
            uploads_path,
            outputs_path,
        )
        return {"messages": [reminder]}

    @override
    def before_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:  # noqa: ARG002
        return self._inject_workspace_message(state)

    @override
    async def abefore_model(self, state: ThreadState, runtime: Runtime) -> dict[str, Any] | None:  # noqa: ARG002
        return self._inject_workspace_message(state)
