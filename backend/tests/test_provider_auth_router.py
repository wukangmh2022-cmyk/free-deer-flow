from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.routers import provider_auth


class _MockAsyncClient:
    def __init__(self, response: httpx.Response | Exception):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(provider_auth.router)
    return app


def test_provider_auth_status_accepts_session_state_or_cookie_store(tmp_path):
    deepseek_session = tmp_path / "deepseek-session.json"
    deepseek_session.write_text('{"chat_url":"https://chat.deepseek.com/a"}', encoding="utf-8")

    xiaomi_profile = tmp_path / "xiaomi-profile" / "Default" / "Network"
    xiaomi_profile.mkdir(parents=True, exist_ok=True)
    (xiaomi_profile / "Cookies").write_bytes(b"sqlite-cookie-db")

    deepseek_spec = provider_auth.ProviderAuthSpec(
        provider="deepseek",
        label="DeepSeek",
        model="deepseek-web-deerflow",
        session_state_path=str(deepseek_session),
        profile_dir=str(tmp_path / "deepseek-profile"),
    )
    xiaomi_spec = provider_auth.ProviderAuthSpec(
        provider="xiaomi",
        label="Xiaomi MiMo",
        model="xiaomi-mimo-v2-pro",
        session_state_path=str(tmp_path / "xiaomi-session.json"),
        profile_dir=str(tmp_path / "xiaomi-profile"),
    )

    with patch.dict(
        provider_auth.PROVIDER_SPECS,
        {"deepseek": deepseek_spec, "xiaomi": xiaomi_spec},
        clear=True,
    ):
        with TestClient(_make_app()) as client:
            response = client.get("/api/provider-auth/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["hasAnyReady"] is True
    assert payload["providers"]["deepseek"]["ready"] is True
    assert payload["providers"]["deepseek"]["has_session_state"] is True
    assert payload["providers"]["deepseek"]["has_cookie_store"] is False
    assert payload["providers"]["xiaomi"]["ready"] is True
    assert payload["providers"]["xiaomi"]["has_session_state"] is False
    assert payload["providers"]["xiaomi"]["has_cookie_store"] is True


def test_provider_auth_status_rejects_invalid_or_empty_session_state(tmp_path):
    session_path = tmp_path / "broken-session.json"
    session_path.write_text("{not-json", encoding="utf-8")
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    spec = provider_auth.ProviderAuthSpec(
        provider="deepseek",
        label="DeepSeek",
        model="deepseek-web-deerflow",
        session_state_path=str(session_path),
        profile_dir=str(profile_dir),
    )

    with patch.dict(provider_auth.PROVIDER_SPECS, {"deepseek": spec}, clear=True):
        with TestClient(_make_app()) as client:
            response = client.get("/api/provider-auth/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["hasAnyReady"] is False
    assert payload["providers"]["deepseek"]["ready"] is False
    assert payload["providers"]["deepseek"]["has_session_state"] is False
    assert payload["providers"]["deepseek"]["has_cookie_store"] is False


def test_open_provider_login_proxies_to_local_provider():
    request = httpx.Request("POST", "http://127.0.0.1:8765/debug/open-login")
    response = httpx.Response(
        200,
        json={"url": "https://chat.deepseek.com/", "headless": False},
        request=request,
    )

    with patch("app.gateway.routers.provider_auth.httpx.AsyncClient", return_value=_MockAsyncClient(response)):
        with TestClient(_make_app()) as client:
            result = client.post("/api/provider-auth/open-login", json={"provider": "deepseek"})

    assert result.status_code == 200
    payload = result.json()
    assert payload["provider"] == "deepseek"
    assert payload["model"] == "deepseek-web-deerflow"
    assert payload["url"] == "https://chat.deepseek.com/"


def test_open_provider_login_returns_502_when_provider_unreachable():
    error = httpx.ConnectError("connection refused")

    with patch("app.gateway.routers.provider_auth.httpx.AsyncClient", return_value=_MockAsyncClient(error)):
        with TestClient(_make_app()) as client:
            result = client.post("/api/provider-auth/open-login", json={"provider": "xiaomi"})

    assert result.status_code == 502
    assert "Provider login service unavailable" in result.json()["detail"]
