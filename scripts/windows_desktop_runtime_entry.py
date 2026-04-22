from __future__ import annotations

import multiprocessing
import os
import sys

import uvicorn


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _run_provider() -> int:
    from app.deepseek_local_provider import app as provider_app

    host = os.environ.get("DEEPSEEK_LOCAL_PROVIDER_HOST", "127.0.0.1")
    port = _int_env("DEEPSEEK_LOCAL_PROVIDER_PORT", 8765)
    uvicorn.run(provider_app, host=host, port=port, log_level=os.environ.get("DEER_FLOW_UVICORN_LOG_LEVEL", "info"))
    return 0


def _run_gateway() -> int:
    from app.gateway.app import create_app

    host = os.environ.get("DEER_FLOW_GATEWAY_HOST", "127.0.0.1")
    port = _int_env("DEER_FLOW_GATEWAY_PORT", 8001)
    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level=os.environ.get("DEER_FLOW_UVICORN_LOG_LEVEL", "info"))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    role = (argv[0] if argv else os.environ.get("DEER_FLOW_DESKTOP_ROLE", "")).strip().lower()
    if role == "provider":
        return _run_provider()
    if role == "gateway":
        return _run_gateway()

    print("Usage: deerflow-runtime.exe [provider|gateway]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
