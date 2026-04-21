from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.routers import workspaces


def _mount(host_path: Path, container_path: str = "/mnt/workspace", read_only: bool = False):
    return type(
        "Mount",
        (),
        {
            "host_path": str(host_path),
            "container_path": container_path,
            "read_only": read_only,
        },
    )()


def _app_with_mounts(mounts: list[object]) -> TestClient:
    app = FastAPI()
    app.include_router(workspaces.router)
    config = type("Config", (), {"sandbox": type("Sandbox", (), {"mounts": mounts})()})()
    return TestClient(app), config


def test_create_workspace_folder_creates_child_directory(tmp_path):
    host_root = tmp_path / "workspace"
    host_root.mkdir()
    parent = host_root / "project"
    parent.mkdir()

    client, config = _app_with_mounts([_mount(host_root)])

    with patch("app.gateway.routers.workspaces.get_app_config", return_value=config):
        response = client.post(
            "/api/workspaces/folders",
            json={"path": str(parent), "name": "notes"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["label"] == "notes"
    assert (parent / "notes").is_dir()


def test_create_workspace_folder_rejects_invalid_name(tmp_path):
    host_root = tmp_path / "workspace"
    host_root.mkdir()

    client, config = _app_with_mounts([_mount(host_root)])

    with patch("app.gateway.routers.workspaces.get_app_config", return_value=config):
        response = client.post(
            "/api/workspaces/folders",
            json={"path": str(host_root), "name": "../escape"},
        )

    assert response.status_code == 422
    assert "path separators" in response.json()["detail"]


def test_create_workspace_folder_rejects_read_only_mount(tmp_path):
    host_root = tmp_path / "workspace"
    host_root.mkdir()

    client, config = _app_with_mounts([_mount(host_root, read_only=True)])

    with patch("app.gateway.routers.workspaces.get_app_config", return_value=config):
        response = client.post(
            "/api/workspaces/folders",
            json={"path": str(host_root), "name": "readonly-child"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "Workspace mount is read-only."
