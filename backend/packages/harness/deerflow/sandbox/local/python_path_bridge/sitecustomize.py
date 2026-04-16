"""Best-effort virtual-path bridge for Python processes launched via local host bash.

When DeerFlow runs in LocalSandboxProvider mode, the `bash` tool executes on the
host. Shell command arguments are rewritten from `/mnt/user-data/...` to the
thread's actual host paths, but Python scripts may still contain hard-coded
`/mnt/user-data/...` paths internally. This module is injected through
`PYTHONPATH` for host-bash Python subprocesses and remaps common filesystem APIs
so those virtual paths keep working.
"""

from __future__ import annotations

import builtins
import glob as _glob
import io
import os
import re
import subprocess
from functools import wraps
from pathlib import Path
from typing import Any

_ENABLED = os.environ.get("DEERFLOW_LOCAL_SANDBOX_PATH_BRIDGE") == "1"

_MAPPINGS = {
    "/mnt/user-data/workspace": os.environ.get("DEERFLOW_LOCAL_SANDBOX_WORKSPACE_PATH", "").strip(),
    "/mnt/user-data/uploads": os.environ.get("DEERFLOW_LOCAL_SANDBOX_UPLOADS_PATH", "").strip(),
    "/mnt/user-data/outputs": os.environ.get("DEERFLOW_LOCAL_SANDBOX_OUTPUTS_PATH", "").strip(),
}
_MAPPINGS = {
    virtual.rstrip("/"): actual
    for virtual, actual in _MAPPINGS.items()
    if actual
}
_SORTED_PREFIXES = sorted(_MAPPINGS, key=len, reverse=True)


def _translate_path(value: Any) -> Any:
    try:
        raw = os.fspath(value)
    except TypeError:
        return value

    if isinstance(raw, bytes):
        return value
    if not isinstance(raw, str):
        return value

    for virtual_prefix in _SORTED_PREFIXES:
        if raw == virtual_prefix or raw.startswith(f"{virtual_prefix}/"):
            actual_root = _MAPPINGS[virtual_prefix]
            suffix = raw[len(virtual_prefix) :].lstrip("/")
            translated = str(Path(actual_root) / suffix) if suffix else actual_root
            if raw.endswith("/") and not translated.endswith(os.sep):
                translated += os.sep
            return translated

    return value


def _translate_command_string(command: str) -> str:
    if "/mnt/user-data" not in command:
        return command

    result = command
    for virtual_prefix in _SORTED_PREFIXES:
        actual_root = _MAPPINGS[virtual_prefix]
        pattern = re.compile(rf"{re.escape(virtual_prefix)}(?=/|$|[\s\"';&|<>()])(?:/[^\s\"';&|<>()]*)?")

        def replace_match(match: re.Match) -> str:
            return str(_translate_path(match.group(0)))

        result = pattern.sub(replace_match, result)
    return result


def _translate_structure(value: Any) -> Any:
    if isinstance(value, str):
        return _translate_command_string(value)
    if isinstance(value, list):
        return [_translate_structure(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_translate_structure(item) for item in value)
    if isinstance(value, dict):
        return {key: _translate_structure(item) for key, item in value.items()}
    return _translate_path(value)


def _wrap_single_path_arg(func):
    @wraps(func)
    def wrapper(path, *args, **kwargs):
        return func(_translate_path(path), *args, **kwargs)

    return wrapper


def _wrap_two_path_args(func):
    @wraps(func)
    def wrapper(src, dst, *args, **kwargs):
        return func(_translate_path(src), _translate_path(dst), *args, **kwargs)

    return wrapper


if _ENABLED and _MAPPINGS:
    builtins.open = _wrap_single_path_arg(builtins.open)
    io.open = _wrap_single_path_arg(io.open)

    os.open = _wrap_single_path_arg(os.open)
    os.stat = _wrap_single_path_arg(os.stat)
    os.lstat = _wrap_single_path_arg(os.lstat)
    os.listdir = _wrap_single_path_arg(os.listdir)
    os.scandir = _wrap_single_path_arg(os.scandir)
    os.mkdir = _wrap_single_path_arg(os.mkdir)
    os.makedirs = _wrap_single_path_arg(os.makedirs)
    os.remove = _wrap_single_path_arg(os.remove)
    os.unlink = _wrap_single_path_arg(os.unlink)
    os.rmdir = _wrap_single_path_arg(os.rmdir)
    os.access = _wrap_single_path_arg(os.access)
    os.chmod = _wrap_single_path_arg(os.chmod)
    os.rename = _wrap_two_path_args(os.rename)
    os.replace = _wrap_two_path_args(os.replace)

    os.path.exists = _wrap_single_path_arg(os.path.exists)
    os.path.isfile = _wrap_single_path_arg(os.path.isfile)
    os.path.isdir = _wrap_single_path_arg(os.path.isdir)
    os.path.lexists = _wrap_single_path_arg(os.path.lexists)

    _glob.glob = _wrap_single_path_arg(_glob.glob)
    _glob.iglob = _wrap_single_path_arg(_glob.iglob)

    _orig_run = subprocess.run
    _orig_check_call = subprocess.check_call
    _orig_check_output = subprocess.check_output
    _orig_popen = subprocess.Popen

    @wraps(_orig_run)
    def _run(*args, **kwargs):
        if args:
            args = (_translate_structure(args[0]),) + args[1:]
        elif "args" in kwargs:
            kwargs["args"] = _translate_structure(kwargs["args"])
        return _orig_run(*args, **kwargs)

    @wraps(_orig_check_call)
    def _check_call(*args, **kwargs):
        if args:
            args = (_translate_structure(args[0]),) + args[1:]
        elif "args" in kwargs:
            kwargs["args"] = _translate_structure(kwargs["args"])
        return _orig_check_call(*args, **kwargs)

    @wraps(_orig_check_output)
    def _check_output(*args, **kwargs):
        if args:
            args = (_translate_structure(args[0]),) + args[1:]
        elif "args" in kwargs:
            kwargs["args"] = _translate_structure(kwargs["args"])
        return _orig_check_output(*args, **kwargs)

    @wraps(_orig_popen)
    def _popen(*args, **kwargs):
        if args:
            args = (_translate_structure(args[0]),) + args[1:]
        elif "args" in kwargs:
            kwargs["args"] = _translate_structure(kwargs["args"])
        return _orig_popen(*args, **kwargs)

    subprocess.run = _run
    subprocess.check_call = _check_call
    subprocess.check_output = _check_output
    subprocess.Popen = _popen
