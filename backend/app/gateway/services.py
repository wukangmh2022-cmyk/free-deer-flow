"""Run lifecycle service layer.

Centralizes the business logic for creating runs, formatting SSE
frames, and consuming stream bridge events.  Router modules
(``thread_runs``, ``runs``) are thin HTTP handlers that delegate here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from fastapi import HTTPException, Request
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.gateway.deps import get_checkpointer, get_run_manager, get_store, get_stream_bridge
from deerflow.runtime import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    ConflictError,
    DisconnectMode,
    RunManager,
    RunRecord,
    RunStatus,
    StreamBridge,
    UnsupportedStrategyError,
    run_agent,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SSE formatting
# ---------------------------------------------------------------------------


def format_sse(event: str, data: Any, *, event_id: str | None = None) -> str:
    """Format a single SSE frame.

    Field order: ``event:`` -> ``data:`` -> ``id:`` (optional) -> blank line.
    This matches the LangGraph Platform wire format consumed by the
    ``useStream`` React hook and the Python ``langgraph-sdk`` SSE decoder.
    """
    payload = json.dumps(data, default=str, ensure_ascii=False)
    parts = [f"event: {event}", f"data: {payload}"]
    if event_id:
        parts.append(f"id: {event_id}")
    parts.append("")
    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Input / config helpers
# ---------------------------------------------------------------------------


def normalize_stream_modes(raw: list[str] | str | None) -> list[str]:
    """Normalize the stream_mode parameter to a list.

    Default matches what ``useStream`` expects: values + messages-tuple.
    """
    if raw is None:
        return ["values"]
    if isinstance(raw, str):
        return [raw]
    return raw if raw else ["values"]


def normalize_input(raw_input: dict[str, Any] | None) -> dict[str, Any]:
    """Convert LangGraph Platform input format to LangChain state dict."""
    if raw_input is None:
        return {}
    messages = raw_input.get("messages")
    if messages and isinstance(messages, list):
        converted = []
        for msg in messages:
            if isinstance(msg, dict):
                role = msg.get("role", msg.get("type", "user"))
                content = msg.get("content", "")
                message_kwargs: dict[str, Any] = {}
                additional_kwargs = msg.get("additional_kwargs")
                if isinstance(additional_kwargs, dict):
                    message_kwargs["additional_kwargs"] = additional_kwargs
                message_id = msg.get("id")
                if isinstance(message_id, str) and message_id:
                    message_kwargs["id"] = message_id
                message_name = msg.get("name")
                if isinstance(message_name, str) and message_name:
                    message_kwargs["name"] = message_name

                if role in ("user", "human"):
                    converted.append(HumanMessage(content=content, **message_kwargs))
                elif role in ("system",):
                    converted.append(SystemMessage(content=content, **message_kwargs))
                elif role in ("assistant", "ai"):
                    tool_calls = msg.get("tool_calls")
                    if isinstance(tool_calls, list):
                        message_kwargs["tool_calls"] = tool_calls
                    converted.append(AIMessage(content=content, **message_kwargs))
                elif role in ("tool",):
                    tool_call_id = msg.get("tool_call_id")
                    if isinstance(tool_call_id, str) and tool_call_id:
                        message_kwargs["tool_call_id"] = tool_call_id
                    converted.append(ToolMessage(content=content, **message_kwargs))
                else:
                    converted.append(HumanMessage(content=content, **message_kwargs))
            else:
                converted.append(msg)
        return {**raw_input, "messages": converted}
    return raw_input


def resolve_disconnect_mode(
    on_disconnect: str | None,
    *,
    stream_resumable: bool | None,
) -> DisconnectMode:
    """Resolve effective disconnect behaviour for a run.

    LangGraph's React ``useStream`` transport treats resumable streams as
    ``onDisconnect="continue"`` by default. Mirror that behavior server-side
    so clients that omit the field still get resumable semantics.
    """
    if on_disconnect == "continue":
        return DisconnectMode.continue_
    if on_disconnect == "cancel":
        return DisconnectMode.cancel
    return DisconnectMode.continue_ if stream_resumable else DisconnectMode.cancel


_DEFAULT_ASSISTANT_ID = "lead_agent"
_THREAD_METADATA_RUNTIME_CONTEXT_KEYS = {
    "workspace_id",
    "workspace_name",
    "workspace_path",
    "workspace_container_path",
}
_THREAD_CONTEXT_CONFIGURABLE_KEYS = {
    "workspace_id",
    "workspace_name",
    "workspace_path",
    "workspace_container_path",
}


def resolve_agent_factory(assistant_id: str | None):
    """Resolve the agent factory callable from config.

    Custom agents are implemented as ``lead_agent`` + an ``agent_name``
    injected into ``configurable`` — see :func:`build_run_config`.  All
    ``assistant_id`` values therefore map to the same factory; the routing
    happens inside ``make_lead_agent`` when it reads ``cfg["agent_name"]``.
    """
    from deerflow.agents.lead_agent.agent import make_lead_agent

    return make_lead_agent


def build_run_config(
    thread_id: str,
    request_config: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    *,
    assistant_id: str | None = None,
) -> dict[str, Any]:
    """Build a RunnableConfig dict for the agent.

    When *assistant_id* refers to a custom agent (anything other than
    ``"lead_agent"`` / ``None``), the name is forwarded as
    ``configurable["agent_name"]``.  ``make_lead_agent`` reads this key to
    load the matching ``agents/<name>/SOUL.md`` and per-agent config —
    without it the agent silently runs as the default lead agent.

    This mirrors the channel manager's ``_resolve_run_params`` logic so that
    the LangGraph Platform-compatible HTTP API and the IM channel path behave
    identically.
    """
    config: dict[str, Any] = {"recursion_limit": 100}
    if request_config:
        # LangGraph >= 0.6.0 introduced ``context`` as the preferred way to
        # pass thread-level data and rejects requests that include both
        # ``configurable`` and ``context``.  If the caller already sends
        # ``context``, honour it and skip our own ``configurable`` dict.
        if "context" in request_config:
            if "configurable" in request_config:
                logger.warning(
                    "build_run_config: client sent both 'context' and 'configurable'; preferring 'context' (LangGraph >= 0.6.0). thread_id=%s, caller_configurable keys=%s",
                    thread_id,
                    list(request_config.get("configurable", {}).keys()),
                )
            config["context"] = request_config["context"]
        else:
            configurable = {"thread_id": thread_id}
            configurable.update(request_config.get("configurable", {}))
            config["configurable"] = configurable
        for k, v in request_config.items():
            if k not in ("configurable", "context"):
                config[k] = v
    else:
        config["configurable"] = {"thread_id": thread_id}

    # Inject custom agent name when the caller specified a non-default assistant.
    # Honour an explicit configurable["agent_name"] in the request if already set.
    if assistant_id and assistant_id != _DEFAULT_ASSISTANT_ID and "configurable" in config:
        if "agent_name" not in config["configurable"]:
            normalized = assistant_id.strip().lower().replace("_", "-")
            if not normalized or not re.fullmatch(r"[a-z0-9-]+", normalized):
                raise ValueError(f"Invalid assistant_id {assistant_id!r}: must contain only letters, digits, and hyphens after normalization.")
            config["configurable"]["agent_name"] = normalized
    if metadata:
        config.setdefault("metadata", {}).update(metadata)
    return config


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------


async def _upsert_thread_in_store(store, thread_id: str, metadata: dict | None) -> None:
    """Create or refresh the thread record in the Store.

    Called from :func:`start_run` so that threads created via the stateless
    ``/runs/stream`` endpoint (which never calls ``POST /threads``) still
    appear in ``/threads/search`` results.
    """
    # Deferred import to avoid circular import with the threads router module.
    from app.gateway.routers.threads import _store_upsert

    try:
        await _store_upsert(store, thread_id, metadata=metadata)
    except Exception:
        logger.warning("Failed to upsert thread %s in store (non-fatal)", thread_id)


async def _sync_thread_title_after_run(
    run_task: asyncio.Task,
    thread_id: str,
    checkpointer: Any,
    store: Any,
) -> None:
    """Wait for *run_task* to finish, then persist the generated title to the Store.

    TitleMiddleware writes the generated title to the LangGraph agent state
    (checkpointer) but the Gateway's Store record is not updated automatically.
    This coroutine closes that gap by reading the final checkpoint after the
    run completes and syncing ``values.title`` into the Store record so that
    subsequent ``/threads/search`` responses include the correct title.

    Runs as a fire-and-forget :func:`asyncio.create_task`; failures are
    logged at DEBUG level and never propagate.
    """
    # Wait for the background run task to complete (any outcome).
    # asyncio.wait does not propagate task exceptions — it just returns
    # when the task is done, cancelled, or failed.
    await asyncio.wait({run_task})

    # Deferred import to avoid circular import with the threads router module.
    from app.gateway.routers.threads import _store_get, _store_put

    try:
        ckpt_config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        ckpt_tuple = await checkpointer.aget_tuple(ckpt_config)
        if ckpt_tuple is None:
            return

        channel_values = ckpt_tuple.checkpoint.get("channel_values", {})
        title = channel_values.get("title")
        if not title:
            return

        existing = await _store_get(store, thread_id)
        if existing is None:
            return

        updated = dict(existing)
        updated.setdefault("values", {})["title"] = title
        updated["updated_at"] = time.time()
        await _store_put(store, updated)
        logger.debug("Synced title %r for thread %s", title, thread_id)
    except Exception:
        logger.debug("Failed to sync title for thread %s (non-fatal)", thread_id, exc_info=True)


async def start_run(
    body: Any,
    thread_id: str,
    request: Request,
) -> RunRecord:
    """Create a RunRecord and launch the background agent task.

    Parameters
    ----------
    body : RunCreateRequest
        The validated request body (typed as Any to avoid circular import
        with the router module that defines the Pydantic model).
    thread_id : str
        Target thread.
    request : Request
        FastAPI request — used to retrieve singletons from ``app.state``.
    """
    bridge = get_stream_bridge(request)
    run_mgr = get_run_manager(request)
    checkpointer = get_checkpointer(request)
    store = get_store(request)

    disconnect = resolve_disconnect_mode(
        body.on_disconnect,
        stream_resumable=body.stream_resumable,
    )

    try:
        record = await run_mgr.create_or_reject(
            thread_id,
            body.assistant_id,
            on_disconnect=disconnect,
            metadata=body.metadata or {},
            kwargs={"input": body.input, "config": body.config},
            multitask_strategy=body.multitask_strategy,
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UnsupportedStrategyError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc

    # Load persisted thread metadata before creating the run so runtime context
    # can inherit bound workspace information even when this request does not
    # repeat workspace_path/workspace_container_path explicitly.
    persisted_thread_metadata: dict[str, Any] = {}
    if store is not None:
        try:
            from app.gateway.routers.threads import _store_get

            thread_record = await _store_get(store, thread_id)
            metadata = thread_record.get("metadata", {}) if isinstance(thread_record, dict) else {}
            if isinstance(metadata, dict):
                persisted_thread_metadata = dict(metadata)
        except Exception:
            logger.debug("Failed to load thread metadata for %s before start_run", thread_id, exc_info=True)

    # Ensure the thread is visible in /threads/search, even for threads that
    # were never explicitly created via POST /threads (e.g. stateless runs).
    store = get_store(request)
    if store is not None:
        await _upsert_thread_in_store(store, thread_id, body.metadata)

    agent_factory = resolve_agent_factory(body.assistant_id)
    graph_input = normalize_input(body.input)
    config = build_run_config(thread_id, body.config, body.metadata, assistant_id=body.assistant_id)

    # Preserve the full DeerFlow context in ``config["context"]`` so thread-level
    # middlewares can access bound workspace paths and other runtime-only data.
    # Agent-relevant keys are also mirrored into ``configurable`` for code paths
    # that still read model settings from config.configurable.
    context = getattr(body, "context", None)
    request_metadata = body.metadata if isinstance(body.metadata, dict) else {}
    runtime_context = config.get("context")
    if not isinstance(runtime_context, dict):
        runtime_context = {}
    else:
        runtime_context = dict(runtime_context)

    for metadata_source in (persisted_thread_metadata, request_metadata):
        for key in _THREAD_METADATA_RUNTIME_CONTEXT_KEYS:
            value = metadata_source.get(key)
            if value is not None:
                runtime_context.setdefault(key, value)

    if context:
        for key, value in context.items():
            if key == "thread_id":
                continue
            runtime_context[key] = value

    if runtime_context or context:
        runtime_context["thread_id"] = thread_id
        config["context"] = runtime_context

    configurable = config.setdefault("configurable", {})
    for key in _THREAD_CONTEXT_CONFIGURABLE_KEYS:
        if key in runtime_context:
            configurable.setdefault(key, runtime_context[key])

    if context:
        _CONTEXT_CONFIGURABLE_KEYS = {
            "agent_name",
            "is_bootstrap",
            "model_name",
            "mode",
            "context_compression_enabled",
            "thinking_enabled",
            "reasoning_effort",
            "is_plan_mode",
            "subagent_enabled",
            "max_concurrent_subagents",
        }
        for key in _CONTEXT_CONFIGURABLE_KEYS:
            if key in context:
                configurable.setdefault(key, context[key])

    stream_modes = normalize_stream_modes(body.stream_mode)

    task = asyncio.create_task(
        run_agent(
            bridge,
            run_mgr,
            record,
            checkpointer=checkpointer,
            store=store,
            agent_factory=agent_factory,
            graph_input=graph_input,
            config=config,
            stream_modes=stream_modes,
            stream_subgraphs=body.stream_subgraphs,
            interrupt_before=body.interrupt_before,
            interrupt_after=body.interrupt_after,
        )
    )
    record.task = task

    # After the run completes, sync the title generated by TitleMiddleware from
    # the checkpointer into the Store record so that /threads/search returns the
    # correct title instead of an empty values dict.
    if store is not None:
        asyncio.create_task(_sync_thread_title_after_run(task, thread_id, checkpointer, store))

    return record


async def sse_consumer(
    bridge: StreamBridge,
    record: RunRecord,
    request: Request,
    run_mgr: RunManager,
):
    """Async generator that yields SSE frames from the bridge.

    The ``finally`` block implements ``on_disconnect`` semantics:
    - ``cancel``: abort the background task on client disconnect.
    - ``continue``: let the task run; events are discarded.
    """
    last_event_id = request.headers.get("Last-Event-ID")
    try:
        async for entry in bridge.subscribe(record.run_id, last_event_id=last_event_id):
            if await request.is_disconnected():
                break

            if entry is HEARTBEAT_SENTINEL:
                yield ": heartbeat\n\n"
                continue

            if entry is END_SENTINEL:
                yield format_sse("end", None, event_id=entry.id or None)
                return

            yield format_sse(entry.event, entry.data, event_id=entry.id or None)

    finally:
        if record.status in (RunStatus.pending, RunStatus.running):
            if record.on_disconnect == DisconnectMode.cancel:
                await run_mgr.cancel(record.run_id)
