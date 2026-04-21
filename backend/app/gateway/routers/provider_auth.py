import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/provider-auth", tags=["provider-auth"])

PROVIDER_HOST = os.environ.get("DEEPSEEK_LOCAL_PROVIDER_HOST", "127.0.0.1")
PROVIDER_PORT = int(os.environ.get("DEEPSEEK_LOCAL_PROVIDER_PORT", "8765"))

DEEPSEEK_SESSION_STATE_PATH = os.environ.get(
    "DEEPSEEK_WEB_SESSION_STATE_DEERFLOW",
    "~/.deerflow/deepseek-web-deerflow-session.json",
)
DEEPSEEK_PROFILE_DIR = os.environ.get(
    "DEEPSEEK_WEB_PROFILE_DEERFLOW",
    "~/.deerflow/profile-deerflow",
)
XIAOMI_SESSION_STATE_PATH = os.environ.get(
    "XIAOMI_MIMO_WEB_SESSION_STATE",
    "~/.deerflow/xiaomi-mimo-session.json",
)
XIAOMI_PROFILE_DIR = os.environ.get(
    "XIAOMI_MIMO_WEB_PROFILE",
    "~/.deerflow/profile-xiaomi-mimo",
)

COOKIE_STORE_CANDIDATES = (
    Path("Cookies"),
    Path("Default") / "Cookies",
    Path("Default") / "Network" / "Cookies",
    Path("Network") / "Cookies",
)


@dataclass(frozen=True)
class ProviderAuthSpec:
    provider: Literal["deepseek", "xiaomi"]
    label: str
    model: str
    session_state_path: str
    profile_dir: str


PROVIDER_SPECS: dict[str, ProviderAuthSpec] = {
    "deepseek": ProviderAuthSpec(
        provider="deepseek",
        label="DeepSeek",
        model="deepseek-web-deerflow",
        session_state_path=DEEPSEEK_SESSION_STATE_PATH,
        profile_dir=DEEPSEEK_PROFILE_DIR,
    ),
    "xiaomi": ProviderAuthSpec(
        provider="xiaomi",
        label="Xiaomi MiMo",
        model="xiaomi-mimo-v2-pro",
        session_state_path=XIAOMI_SESSION_STATE_PATH,
        profile_dir=XIAOMI_PROFILE_DIR,
    ),
}


class ProviderStatusResponse(BaseModel):
    provider: str = Field(..., description="Provider key used by the UI.")
    label: str = Field(..., description="Human-readable provider name.")
    model: str = Field(..., description="Model identifier used to open the login page.")
    ready: bool = Field(..., description="Whether a reusable local login/session was detected.")
    has_session_state: bool = Field(..., description="Whether a persisted session state file was found and parsed.")
    has_cookie_store: bool = Field(..., description="Whether a browser cookie store exists in the profile directory.")
    session_state_path: str = Field(..., description="Resolved path to the persisted session state file.")
    profile_dir: str = Field(..., description="Resolved path to the persistent browser profile directory.")


class ProviderStatusListResponse(BaseModel):
    has_any_ready: bool = Field(..., alias="hasAnyReady")
    providers: dict[str, ProviderStatusResponse]

    model_config = {"populate_by_name": True}


class OpenLoginRequest(BaseModel):
    provider: Literal["deepseek", "xiaomi"]


def _resolve_path(path_value: str) -> Path:
    return Path(path_value).expanduser().resolve()


def _has_valid_session_state(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(payload, dict) and bool(payload)


def _has_cookie_store(profile_dir: Path) -> bool:
    if not profile_dir.exists():
        return False
    for candidate in COOKIE_STORE_CANDIDATES:
        cookie_file = profile_dir / candidate
        if cookie_file.is_file() and cookie_file.stat().st_size > 0:
            return True
    return False


def _build_provider_status(spec: ProviderAuthSpec) -> ProviderStatusResponse:
    session_state_path = _resolve_path(spec.session_state_path)
    profile_dir = _resolve_path(spec.profile_dir)
    has_session_state = _has_valid_session_state(session_state_path)
    has_cookie_store = _has_cookie_store(profile_dir)
    return ProviderStatusResponse(
        provider=spec.provider,
        label=spec.label,
        model=spec.model,
        ready=has_session_state or has_cookie_store,
        has_session_state=has_session_state,
        has_cookie_store=has_cookie_store,
        session_state_path=str(session_state_path),
        profile_dir=str(profile_dir),
    )


def _provider_base_url() -> str:
    return f"http://{PROVIDER_HOST}:{PROVIDER_PORT}"


async def _open_provider_login(spec: ProviderAuthSpec) -> dict[str, Any]:
    endpoint = f"{_provider_base_url()}/debug/open-login"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
            response = await client.post(endpoint, params={"model": spec.model})
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Provider login service unavailable: {exc}",
        ) from exc

    try:
        payload = response.json()
    except ValueError:
        payload = {"detail": response.text}

    if response.is_error:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        raise HTTPException(
            status_code=502,
            detail=detail or f"Provider login request failed with HTTP {response.status_code}",
        )

    if isinstance(payload, dict):
        return payload
    return {"result": payload}


@router.get(
    "/status",
    response_model=ProviderStatusListResponse,
    summary="Get Provider Login Status",
    description="Inspect local persisted provider sessions so the desktop landing page can gate workspace entry.",
)
async def get_provider_status() -> ProviderStatusListResponse:
    providers = {
        name: _build_provider_status(spec)
        for name, spec in PROVIDER_SPECS.items()
    }
    return ProviderStatusListResponse(
        hasAnyReady=any(provider.ready for provider in providers.values()),
        providers=providers,
    )


@router.post(
    "/open-login",
    summary="Open Provider Login",
    description="Proxy a request to the local provider service to open a visible login browser window.",
)
async def open_provider_login(request: OpenLoginRequest) -> dict[str, Any]:
    spec = PROVIDER_SPECS[request.provider]
    payload = await _open_provider_login(spec)
    return {
        "provider": spec.provider,
        "label": spec.label,
        "model": spec.model,
        **payload,
    }
