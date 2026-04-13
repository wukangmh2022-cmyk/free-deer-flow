from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.gateway.deps import get_store
from app.gateway.routers.threads import THREADS_NS
from deerflow.config.paths import get_paths
from deerflow.sandbox.search import should_ignore_name

router = APIRouter(prefix="/api/threads", tags=["thread-files"])

ThreadFileScope = Literal["workspace", "uploads", "outputs"]


class ThreadFileEntry(BaseModel):
    name: str
    relative_path: str
    host_path: str
    is_dir: bool
    size: int | None = None
    modified_at: float | None = None


class ThreadFileScopeData(BaseModel):
    scope: ThreadFileScope
    root_path: str
    mapped_to_workspace: bool = False
    entries: list[ThreadFileEntry] = Field(default_factory=list)


class ThreadFilesResponse(BaseModel):
    workspace_target_path: str | None = None
    scopes: list[ThreadFileScopeData] = Field(default_factory=list)


class MirrorThreadFilesRequest(BaseModel):
    scope: ThreadFileScope = Field(description="Which thread directory to mirror")
    destination_path: str | None = Field(
        default=None,
        description="Optional absolute host directory. Defaults to the thread's selected workspace path.",
    )


class MirrorThreadFilesResponse(BaseModel):
    success: bool
    scope: ThreadFileScope
    source_path: str
    destination_path: str
    mirrored_files: int
    message: str


async def _load_thread_metadata(request: Request, thread_id: str) -> dict:
    store = get_store(request)
    if store is None:
        return {}
    item = await store.aget(THREADS_NS, thread_id)
    if item is None:
        return {}
    value = item.value if isinstance(item.value, dict) else {}
    metadata = value.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _scope_root(thread_id: str, scope: ThreadFileScope) -> Path:
    paths = get_paths()
    if scope == "workspace":
        return paths.sandbox_work_dir(thread_id).resolve()
    if scope == "uploads":
        return paths.sandbox_uploads_dir(thread_id).resolve()
    return paths.sandbox_outputs_dir(thread_id).resolve()


def _iter_entries(root: Path, *, max_depth: int) -> list[ThreadFileEntry]:
    if not root.exists() or not root.is_dir():
        return []

    entries: list[ThreadFileEntry] = []

    def walk(current: Path, depth: int) -> None:
        if depth > max_depth:
            return

        try:
            children = sorted(current.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except OSError:
            return

        for child in children:
            if should_ignore_name(child.name):
                continue
            try:
                stat = child.stat()
            except OSError:
                stat = None
            relative_path = child.relative_to(root).as_posix()
            entries.append(
                ThreadFileEntry(
                    name=child.name,
                    relative_path=relative_path,
                    host_path=str(child.resolve()),
                    is_dir=child.is_dir(),
                    size=None if child.is_dir() or stat is None else stat.st_size,
                    modified_at=None if stat is None else stat.st_mtime,
                )
            )
            if child.is_dir() and depth < max_depth:
                walk(child, depth + 1)

    walk(root, 1)
    return entries


def _copy_tree(source: Path, destination: Path) -> int:
    mirrored_files = 0
    destination.mkdir(parents=True, exist_ok=True)

    for item in source.rglob("*"):
        if should_ignore_name(item.name):
            continue
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        mirrored_files += 1

    return mirrored_files


@router.get("/{thread_id}/files", response_model=ThreadFilesResponse)
async def list_thread_files(thread_id: str, request: Request, max_depth: int = 3) -> ThreadFilesResponse:
    metadata = await _load_thread_metadata(request, thread_id)
    workspace_target_path = metadata.get("workspace_path")
    scopes: list[ThreadFileScopeData] = []

    for scope in ("workspace", "uploads", "outputs"):
        root = _scope_root(thread_id, scope)
        scopes.append(
            ThreadFileScopeData(
                scope=scope,
                root_path=str(root),
                mapped_to_workspace=False,
                entries=_iter_entries(root, max_depth=max(1, min(max_depth, 6))),
            )
        )

    return ThreadFilesResponse(
        workspace_target_path=workspace_target_path if isinstance(workspace_target_path, str) and workspace_target_path.strip() else None,
        scopes=scopes,
    )


@router.post("/{thread_id}/files/mirror", response_model=MirrorThreadFilesResponse)
async def mirror_thread_files(thread_id: str, body: MirrorThreadFilesRequest, request: Request) -> MirrorThreadFilesResponse:
    metadata = await _load_thread_metadata(request, thread_id)
    workspace_target_path = metadata.get("workspace_path")

    source_root = _scope_root(thread_id, body.scope)
    if not source_root.exists() or not source_root.is_dir():
        raise HTTPException(status_code=404, detail=f"Source directory not found for scope '{body.scope}'.")

    destination_base_raw = body.destination_path or workspace_target_path
    if not isinstance(destination_base_raw, str) or not destination_base_raw.strip():
        raise HTTPException(status_code=400, detail="No destination workspace is configured for this thread.")

    destination_base = Path(destination_base_raw).expanduser().resolve()
    if not destination_base.exists() or not destination_base.is_dir():
        raise HTTPException(status_code=404, detail="Destination workspace path does not exist or is not a directory.")

    if body.scope == "workspace":
        destination_root = destination_base
    else:
        destination_root = destination_base

    mirrored_files = _copy_tree(source_root, destination_root)
    return MirrorThreadFilesResponse(
        success=True,
        scope=body.scope,
        source_path=str(source_root),
        destination_path=str(destination_root),
        mirrored_files=mirrored_files,
        message=f"Mirrored {mirrored_files} file(s) from {body.scope} to {destination_root}.",
    )
