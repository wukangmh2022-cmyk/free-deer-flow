from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from deerflow.community.aio_sandbox.aio_sandbox import AioSandbox
from deerflow.sandbox.local.local_sandbox import LocalSandbox
from deerflow.sandbox.search import GrepMatch, find_glob_matches, find_grep_matches
from deerflow.sandbox.tools import glob_tool, grep_tool, ls_tool


def _make_runtime(tmp_path):
    workspace = tmp_path / "workspace"
    uploads = tmp_path / "uploads"
    outputs = tmp_path / "outputs"
    workspace.mkdir()
    uploads.mkdir()
    outputs.mkdir()
    return SimpleNamespace(
        state={
            "sandbox": {"sandbox_id": "local"},
            "thread_data": {
                "workspace_path": str(workspace),
                "uploads_path": str(uploads),
                "outputs_path": str(outputs),
            },
        },
        context={"thread_id": "thread-1"},
    )


def _make_bound_workspace_runtime(tmp_path):
    workspace = tmp_path / "workspace"
    uploads = tmp_path / "uploads"
    outputs = tmp_path / "outputs"
    workspace.mkdir()
    uploads.mkdir()
    outputs.mkdir()
    return SimpleNamespace(
        state={
            "sandbox": {"sandbox_id": "sandbox-1"},
            "thread_data": {
                "workspace_path": str(workspace),
                "workspace_container_path": "/mnt/workspaces/demo",
                "uploads_path": str(uploads),
                "outputs_path": str(outputs),
            },
        },
        context={"thread_id": "thread-1"},
    )


def test_glob_tool_returns_virtual_paths_and_ignores_common_dirs(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "util.py").write_text("print('util')\n", encoding="utf-8")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "skip.py").write_text("ignored\n", encoding="utf-8")

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))

    result = glob_tool.func(
        runtime=runtime,
        description="find python files",
        pattern="**/*.py",
        path="/mnt/user-data/workspace",
    )

    assert "/mnt/user-data/workspace/app.py" in result
    assert "/mnt/user-data/workspace/pkg/util.py" in result
    assert "node_modules" not in result
    assert str(workspace) not in result


def test_glob_tool_maps_bound_host_workspace_path_to_container_path_for_non_local_runtime(tmp_path, monkeypatch) -> None:
    runtime = _make_bound_workspace_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    captured: dict[str, object] = {}

    class FakeSandbox:
        def glob(self, path, pattern, include_dirs=False, max_results=200):
            captured["path"] = path
            captured["pattern"] = pattern
            captured["include_dirs"] = include_dirs
            captured["max_results"] = max_results
            return ["/mnt/workspaces/demo/overtime.csv"], False

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: FakeSandbox())
    monkeypatch.setattr("deerflow.sandbox.tools.is_local_sandbox", lambda runtime: False)

    result = glob_tool.func(
        runtime=runtime,
        description="find overtime files",
        pattern="**/*.{csv,xlsx,xls}",
        path=str(workspace),
    )

    assert captured["path"] == "/mnt/workspaces/demo"
    assert "/mnt/workspaces/demo/overtime.csv" in result
    assert f"under {workspace}" in result


def test_glob_tool_resolves_custom_mount_container_path_in_local_runtime(tmp_path, monkeypatch) -> None:
    runtime = _make_bound_workspace_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "nested").mkdir()
    (workspace / "nested" / "overtime.csv").write_text("ok\n", encoding="utf-8")
    mount = SimpleNamespace(
        host_path=str(workspace),
        container_path="/mnt/workspaces/demo",
        read_only=False,
    )

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))
    monkeypatch.setattr("deerflow.sandbox.tools.is_local_sandbox", lambda runtime: True)
    monkeypatch.setattr("deerflow.sandbox.tools._get_custom_mounts", lambda: [mount])

    result = glob_tool.func(
        runtime=runtime,
        description="find overtime files in custom mount",
        pattern="**/*.csv",
        path="/mnt/workspaces/demo",
    )

    assert "/mnt/workspaces/demo/nested/overtime.csv" in result
    assert str(workspace) not in result


def test_ls_tool_maps_bound_host_workspace_path_to_container_path_for_non_local_runtime(tmp_path, monkeypatch) -> None:
    runtime = _make_bound_workspace_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    captured: dict[str, object] = {}

    class FakeSandbox:
        def list_dir(self, path, max_depth=4):
            captured["path"] = path
            captured["max_depth"] = max_depth
            return ["/mnt/workspaces/demo/data", "/mnt/workspaces/demo/report.csv"]

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: FakeSandbox())
    monkeypatch.setattr("deerflow.sandbox.tools.is_local_sandbox", lambda runtime: False)

    result = ls_tool.func(
        runtime=runtime,
        description="inspect bound workspace",
        path=str(workspace),
    )

    assert captured["path"] == "/mnt/workspaces/demo"
    assert "/mnt/workspaces/demo/report.csv" in result


def test_glob_tool_accepts_bound_host_workspace_path(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "analysis.py").write_text("print('ok')\n", encoding="utf-8")

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))

    result = glob_tool.func(
        runtime=runtime,
        description="find python files from host path",
        pattern="**/*.py",
        path=str(workspace),
    )

    assert str(workspace) in result
    assert "/mnt/user-data/workspace/analysis.py" in result


def test_glob_tool_supports_skills_virtual_paths(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    skills_dir = tmp_path / "skills"
    (skills_dir / "public" / "demo").mkdir(parents=True)
    (skills_dir / "public" / "demo" / "SKILL.md").write_text("# Demo\n", encoding="utf-8")

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))

    with (
        patch("deerflow.sandbox.tools._get_skills_container_path", return_value="/mnt/skills"),
        patch("deerflow.sandbox.tools._get_skills_host_path", return_value=str(skills_dir)),
    ):
        result = glob_tool.func(
            runtime=runtime,
            description="find skills",
            pattern="**/SKILL.md",
            path="/mnt/skills",
        )

    assert "/mnt/skills/public/demo/SKILL.md" in result
    assert str(skills_dir) not in result


def test_grep_tool_filters_by_glob_and_skips_binary_files(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "main.py").write_text("TODO = 'ship it'\nprint(TODO)\n", encoding="utf-8")
    (workspace / "notes.txt").write_text("TODO in txt should be filtered\n", encoding="utf-8")
    (workspace / "image.bin").write_bytes(b"\0binary TODO")

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))

    result = grep_tool.func(
        runtime=runtime,
        description="find todo references",
        pattern="TODO",
        path="/mnt/user-data/workspace",
        glob="**/*.py",
    )

    assert "/mnt/user-data/workspace/main.py:1: TODO = 'ship it'" in result
    assert "notes.txt" not in result
    assert "image.bin" not in result
    assert str(workspace) not in result


def test_grep_tool_resolves_custom_mount_container_path_in_local_runtime(tmp_path, monkeypatch) -> None:
    runtime = _make_bound_workspace_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "main.py").write_text("TODO = 'ship it'\n", encoding="utf-8")
    mount = SimpleNamespace(
        host_path=str(workspace),
        container_path="/mnt/workspaces/demo",
        read_only=False,
    )

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))
    monkeypatch.setattr("deerflow.sandbox.tools.is_local_sandbox", lambda runtime: True)
    monkeypatch.setattr("deerflow.sandbox.tools._get_custom_mounts", lambda: [mount])

    result = grep_tool.func(
        runtime=runtime,
        description="find todo references in custom mount",
        pattern="TODO",
        path="/mnt/workspaces/demo",
    )

    assert "/mnt/workspaces/demo/main.py:1: TODO = 'ship it'" in result
    assert str(workspace) not in result


def test_grep_tool_truncates_results(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "main.py").write_text("TODO one\nTODO two\nTODO three\n", encoding="utf-8")

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))
    # Prevent config.yaml tool config from overriding the caller-supplied max_results=2.
    monkeypatch.setattr("deerflow.sandbox.tools.get_app_config", lambda: SimpleNamespace(get_tool_config=lambda name: None))

    result = grep_tool.func(
        runtime=runtime,
        description="limit matches",
        pattern="TODO",
        path="/mnt/user-data/workspace",
        max_results=2,
    )

    assert "Found 2 matches under /mnt/user-data/workspace (showing first 2)" in result
    assert "TODO one" in result
    assert "TODO two" in result
    assert "TODO three" not in result
    assert "Results truncated." in result


def test_glob_tool_include_dirs_filters_nested_ignored_paths(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir()
    (workspace / "src" / "main.py").write_text("x\n", encoding="utf-8")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "lib").mkdir()

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))

    result = glob_tool.func(
        runtime=runtime,
        description="find dirs",
        pattern="**",
        path="/mnt/user-data/workspace",
        include_dirs=True,
    )

    assert "src" in result
    assert "node_modules" not in result


def test_grep_tool_literal_mode(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "file.py").write_text("price = (a+b)\nresult = a+b\n", encoding="utf-8")

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))

    # literal=True should treat (a+b) as a plain string, not a regex group
    result = grep_tool.func(
        runtime=runtime,
        description="literal search",
        pattern="(a+b)",
        path="/mnt/user-data/workspace",
        literal=True,
    )

    assert "price = (a+b)" in result
    assert "result = a+b" not in result


def test_grep_tool_case_sensitive(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "file.py").write_text("TODO: fix\ntodo: also fix\n", encoding="utf-8")

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))

    result = grep_tool.func(
        runtime=runtime,
        description="case sensitive search",
        pattern="TODO",
        path="/mnt/user-data/workspace",
        case_sensitive=True,
    )

    assert "TODO: fix" in result
    assert "todo: also fix" not in result


def test_grep_tool_invalid_regex_returns_error(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))

    result = grep_tool.func(
        runtime=runtime,
        description="bad pattern",
        pattern="[invalid",
        path="/mnt/user-data/workspace",
    )

    assert "Invalid regex pattern" in result


def test_aio_sandbox_glob_include_dirs_filters_nested_ignored(monkeypatch) -> None:
    with patch("deerflow.community.aio_sandbox.aio_sandbox.AioSandboxClient"):
        sandbox = AioSandbox(id="test-sandbox", base_url="http://localhost:8080")
    monkeypatch.setattr(
        sandbox._client.file,
        "list_path",
        lambda **kwargs: SimpleNamespace(
            data=SimpleNamespace(
                files=[
                    SimpleNamespace(name="src", path="/mnt/workspace/src"),
                    SimpleNamespace(name="node_modules", path="/mnt/workspace/node_modules"),
                    # child of node_modules — should be filtered via should_ignore_path
                    SimpleNamespace(name="lib", path="/mnt/workspace/node_modules/lib"),
                ]
            )
        ),
    )

    matches, truncated = sandbox.glob("/mnt/workspace", "**", include_dirs=True)

    assert "/mnt/workspace/src" in matches
    assert "/mnt/workspace/node_modules" not in matches
    assert "/mnt/workspace/node_modules/lib" not in matches
    assert truncated is False


def test_aio_sandbox_grep_invalid_regex_raises() -> None:
    with patch("deerflow.community.aio_sandbox.aio_sandbox.AioSandboxClient"):
        sandbox = AioSandbox(id="test-sandbox", base_url="http://localhost:8080")

    import re

    try:
        sandbox.grep("/mnt/workspace", "[invalid")
        assert False, "Expected re.error"
    except re.error:
        pass


def test_aio_sandbox_glob_parses_json(monkeypatch) -> None:
    with patch("deerflow.community.aio_sandbox.aio_sandbox.AioSandboxClient"):
        sandbox = AioSandbox(id="test-sandbox", base_url="http://localhost:8080")
    monkeypatch.setattr(
        sandbox._client.file,
        "find_files",
        lambda **kwargs: SimpleNamespace(data=SimpleNamespace(files=["/mnt/user-data/workspace/app.py", "/mnt/user-data/workspace/node_modules/skip.py"])),
    )

    matches, truncated = sandbox.glob("/mnt/user-data/workspace", "**/*.py")

    assert matches == ["/mnt/user-data/workspace/app.py"]
    assert truncated is False


def test_aio_sandbox_grep_parses_json(monkeypatch) -> None:
    with patch("deerflow.community.aio_sandbox.aio_sandbox.AioSandboxClient"):
        sandbox = AioSandbox(id="test-sandbox", base_url="http://localhost:8080")
    monkeypatch.setattr(
        sandbox._client.file,
        "list_path",
        lambda **kwargs: SimpleNamespace(
            data=SimpleNamespace(
                files=[
                    SimpleNamespace(
                        name="app.py",
                        path="/mnt/user-data/workspace/app.py",
                        is_directory=False,
                    )
                ]
            )
        ),
    )
    monkeypatch.setattr(
        sandbox._client.file,
        "search_in_file",
        lambda **kwargs: SimpleNamespace(data=SimpleNamespace(line_numbers=[7], matches=["TODO = True"])),
    )

    matches, truncated = sandbox.grep("/mnt/user-data/workspace", "TODO")

    assert matches == [GrepMatch(path="/mnt/user-data/workspace/app.py", line_number=7, line="TODO = True")]
    assert truncated is False


def test_find_glob_matches_raises_not_a_directory(tmp_path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("x\n", encoding="utf-8")

    try:
        find_glob_matches(file_path, "**/*.py")
        assert False, "Expected NotADirectoryError"
    except NotADirectoryError:
        pass


def test_find_glob_matches_supports_brace_expansion(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.cpp").write_text("// cpp\n", encoding="utf-8")
    (workspace / "main.h").write_text("// h\n", encoding="utf-8")
    (workspace / "note.txt").write_text("ignore\n", encoding="utf-8")

    matches, truncated = find_glob_matches(workspace, "**/*.{c,cpp,h,hpp}")

    assert truncated is False
    match_names = sorted([Path(item).name for item in matches])
    assert match_names == ["main.cpp", "main.h"]


def test_find_grep_matches_raises_not_a_directory(tmp_path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("TODO\n", encoding="utf-8")

    try:
        find_grep_matches(file_path, "TODO")
        assert False, "Expected NotADirectoryError"
    except NotADirectoryError:
        pass


def test_find_grep_matches_skips_symlink_outside_root(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("TODO outside\n", encoding="utf-8")
    (workspace / "outside-link.txt").symlink_to(outside)

    matches, truncated = find_grep_matches(workspace, "TODO")

    assert matches == []
    assert truncated is False


def test_glob_tool_honors_smaller_requested_max_results(tmp_path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "a.py").write_text("print('a')\n", encoding="utf-8")
    (workspace / "b.py").write_text("print('b')\n", encoding="utf-8")
    (workspace / "c.py").write_text("print('c')\n", encoding="utf-8")

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: LocalSandbox(id="local"))
    monkeypatch.setattr(
        "deerflow.sandbox.tools.get_app_config",
        lambda: SimpleNamespace(get_tool_config=lambda name: SimpleNamespace(model_extra={"max_results": 50})),
    )

    result = glob_tool.func(
        runtime=runtime,
        description="limit glob matches",
        pattern="**/*.py",
        path="/mnt/user-data/workspace",
        max_results=2,
    )

    assert "Found 2 paths under /mnt/user-data/workspace (showing first 2)" in result
    assert "Results truncated." in result


def test_aio_sandbox_glob_include_dirs_enforces_root_boundary(monkeypatch) -> None:
    with patch("deerflow.community.aio_sandbox.aio_sandbox.AioSandboxClient"):
        sandbox = AioSandbox(id="test-sandbox", base_url="http://localhost:8080")
    monkeypatch.setattr(
        sandbox._client.file,
        "list_path",
        lambda **kwargs: SimpleNamespace(
            data=SimpleNamespace(
                files=[
                    SimpleNamespace(name="src", path="/mnt/workspace/src"),
                    SimpleNamespace(name="src2", path="/mnt/workspace2/src2"),
                ]
            )
        ),
    )

    matches, truncated = sandbox.glob("/mnt/workspace", "**", include_dirs=True)

    assert matches == ["/mnt/workspace/src"]
    assert truncated is False


def test_aio_sandbox_grep_skips_mismatched_line_number_payloads(monkeypatch) -> None:
    with patch("deerflow.community.aio_sandbox.aio_sandbox.AioSandboxClient"):
        sandbox = AioSandbox(id="test-sandbox", base_url="http://localhost:8080")
    monkeypatch.setattr(
        sandbox._client.file,
        "list_path",
        lambda **kwargs: SimpleNamespace(
            data=SimpleNamespace(
                files=[
                    SimpleNamespace(
                        name="app.py",
                        path="/mnt/user-data/workspace/app.py",
                        is_directory=False,
                    )
                ]
            )
        ),
    )
    monkeypatch.setattr(
        sandbox._client.file,
        "search_in_file",
        lambda **kwargs: SimpleNamespace(data=SimpleNamespace(line_numbers=[7], matches=["TODO = True", "extra"])),
    )

    matches, truncated = sandbox.grep("/mnt/user-data/workspace", "TODO")

    assert matches == [GrepMatch(path="/mnt/user-data/workspace/app.py", line_number=7, line="TODO = True")]
    assert truncated is False
