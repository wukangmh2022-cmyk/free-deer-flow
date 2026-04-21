from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from deerflow.config import get_app_config

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])

_RESERVED_PREFIXES = {"/mnt/user-data", "/mnt/skills", "/mnt/acp-workspace"}


class WorkspaceItem(BaseModel):
    id: str = Field(description="Stable workspace identifier")
    label: str = Field(description="Display label for the workspace")
    host_path: str = Field(description="Absolute host filesystem path")
    container_path: str = Field(description="Sandbox-visible virtual path")
    read_only: bool = Field(default=False, description="Whether the workspace is read-only")
    source: str = Field(description="How this workspace was discovered")


class WorkspaceListResponse(BaseModel):
    workspaces: list[WorkspaceItem]


class WorkspaceBrowseResponse(BaseModel):
    current: WorkspaceItem
    parent: WorkspaceItem | None = None
    children: list[WorkspaceItem]
    entries: list[WorkspaceItem] = Field(default_factory=list)


class WorkspaceCreateFolderRequest(BaseModel):
    path: str = Field(description="Absolute host path of the parent directory.")
    name: str = Field(description="Folder name to create under the given path.")


def _safe_workspace_id(path: Path) -> str:
    return str(path).replace("/", "_").replace("\\", "_").strip("_") or "workspace"


def _iter_mount_roots() -> list[tuple[Path, str, bool]]:
    config = get_app_config()
    mounts = list(getattr(config.sandbox, "mounts", []) or [])
    roots: list[tuple[Path, str, bool]] = []

    for mount in mounts:
        host_root = Path(mount.host_path).expanduser().resolve()
        container_root = mount.container_path.rstrip("/") or "/"

        if container_root in _RESERVED_PREFIXES:
            continue
        if any(
            container_root == prefix or container_root.startswith(prefix + "/")
            for prefix in _RESERVED_PREFIXES
        ):
            continue
        if not host_root.exists() or not host_root.is_dir():
            continue
        roots.append((host_root, container_root, mount.read_only))

    return roots


def _workspace_item_for_path(
    path: Path,
    *,
    host_root: Path,
    container_root: str,
    read_only: bool,
) -> WorkspaceItem:
    rel = path.relative_to(host_root)
    container_path = container_root if rel == Path(".") else f"{container_root}/{rel.as_posix()}"
    return WorkspaceItem(
        id=_safe_workspace_id(path),
        label=path.name if path != host_root else host_root.name,
        host_path=str(path),
        container_path=container_path,
        read_only=read_only,
        source="sandbox.mounts",
    )


def _resolve_mount_for_path(path: Path) -> tuple[Path, str, bool] | None:
    best_match: tuple[Path, str, bool] | None = None
    for host_root, container_root, read_only in _iter_mount_roots():
        try:
            path.relative_to(host_root)
        except ValueError:
            continue
        if best_match is None or len(str(host_root)) > len(str(best_match[0])):
            best_match = (host_root, container_root, read_only)
    return best_match


def _validate_child_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise HTTPException(status_code=422, detail="Folder name cannot be empty.")
    if normalized in {".", ".."}:
        raise HTTPException(status_code=422, detail="Invalid folder name.")
    if "/" in normalized or "\\" in normalized:
        raise HTTPException(status_code=422, detail="Folder name cannot contain path separators.")
    return normalized


def _iter_workspace_candidates() -> list[WorkspaceItem]:
    items: list[WorkspaceItem] = []
    seen: set[str] = set()

    for host_root, container_root, read_only in _iter_mount_roots():
        roots = [host_root]
        # Add one level of child directories for a Codex-like project picker.
        try:
            roots.extend(
                child
                for child in sorted(host_root.iterdir(), key=lambda p: p.name.lower())
                if child.is_dir() and not child.name.startswith(".")
            )
        except OSError:
            pass

        for candidate in roots:
            host_path = str(candidate.resolve())
            if host_path in seen:
                continue
            seen.add(host_path)
            items.append(_workspace_item_for_path(candidate.resolve(), host_root=host_root, container_root=container_root, read_only=read_only))

    return items


@router.get("", response_model=WorkspaceListResponse)
async def list_workspaces() -> WorkspaceListResponse:
    return WorkspaceListResponse(workspaces=_iter_workspace_candidates())


@router.get("/browse", response_model=WorkspaceBrowseResponse)
async def browse_workspaces(path: str = Query(..., description="Absolute host path of the directory to browse")) -> WorkspaceBrowseResponse:
    requested = Path(path).expanduser().resolve()
    mount = _resolve_mount_for_path(requested)
    if mount is None:
        raise HTTPException(status_code=404, detail="Workspace path is not within a configured mount.")

    host_root, container_root, read_only = mount
    if not requested.exists() or not requested.is_dir():
        raise HTTPException(status_code=404, detail="Workspace path does not exist or is not a directory.")

    children: list[WorkspaceItem] = []
    entries: list[WorkspaceItem] = []
    try:
        child_items = sorted(
            (
                child.resolve()
                for child in requested.iterdir()
                if not child.name.startswith(".")
            ),
            key=lambda p: (not p.is_dir(), p.name.lower()),
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to browse workspace: {exc}") from exc

    for child in child_items:
        item = _workspace_item_for_path(
            child,
            host_root=host_root,
            container_root=container_root,
            read_only=read_only,
        )
        entries.append(item)
        if child.is_dir():
            children.append(item)

    parent: WorkspaceItem | None = None
    if requested != host_root:
        parent_path = requested.parent
        parent = _workspace_item_for_path(
            parent_path,
            host_root=host_root,
            container_root=container_root,
            read_only=read_only,
        )

    return WorkspaceBrowseResponse(
        current=_workspace_item_for_path(
            requested,
            host_root=host_root,
            container_root=container_root,
            read_only=read_only,
        ),
        parent=parent,
        children=children,
        entries=entries,
    )


@router.post("/folders", response_model=WorkspaceItem)
async def create_workspace_folder(
    request: WorkspaceCreateFolderRequest,
) -> WorkspaceItem:
    parent = Path(request.path).expanduser().resolve()
    mount = _resolve_mount_for_path(parent)
    if mount is None:
        raise HTTPException(status_code=404, detail="Workspace path is not within a configured mount.")

    host_root, container_root, read_only = mount
    if read_only:
        raise HTTPException(status_code=403, detail="Workspace mount is read-only.")
    if not parent.exists() or not parent.is_dir():
        raise HTTPException(status_code=404, detail="Workspace path does not exist or is not a directory.")

    child_name = _validate_child_name(request.name)
    destination = (parent / child_name).resolve()

    try:
        destination.relative_to(host_root)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Folder path escapes the configured workspace mount.") from exc

    if destination.exists():
        raise HTTPException(status_code=409, detail="Folder already exists.")

    try:
        destination.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="Folder already exists.") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to create folder: {exc}") from exc

    return _workspace_item_for_path(
        destination,
        host_root=host_root,
        container_root=container_root,
        read_only=read_only,
    )
