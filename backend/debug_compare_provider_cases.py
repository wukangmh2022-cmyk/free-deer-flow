from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


BASE_URL = "http://127.0.0.1:8765"
MODEL = "deepseek-web-deerflow"
NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write text content to a file",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
}


BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a shell command",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["command"],
        },
    },
}


def build_large_python_fixture() -> str:
    lines = [
        "# CASE_100 fixture",
        "import json",
        "import re",
        "from pathlib import Path",
        "",
        'ROOT = Path(r"C:\\\\tmp\\\\provider_case_100")',
        'PATTERN = re.compile(r"^item\\[(\\d+)\\]\\\\value=(.*)$")',
        "",
        "def emit(index: int) -> str:",
        '    payload = {"index": index, "text": f"value-{index}", "quoted": "\\"Q\\"", "path": r"C:\\\\demo\\\\file.txt"}',
        '    return json.dumps(payload, ensure_ascii=False)',
        "",
    ]
    for i in range(1, 111):
        lines.extend(
            [
                f"def step_{i:03d}(raw: str) -> str:",
                f'    marker = "CASE_{i:03d}"',
                r'    escaped = raw.replace("\\", "\\\\").replace("\"", "\\\"")',
                f'    return f"{i:03d}|{{marker}}|{{escaped}}|{{emit({i})}}"',
                "",
            ]
        )
    lines.extend(
        [
            "def main() -> None:",
            '    sample = "alpha\\\\beta\\n\\"quoted\\""',
            "    outputs = []",
            "    for idx in range(1, 6):",
            '        outputs.append(globals()[f"step_{idx:03d}"](sample))',
            '    print("\\n".join(outputs))',
            "",
            'if __name__ == "__main__":',
            "    main()",
        ]
    )
    return "\n".join(lines)


LARGE_PYTHON_FIXTURE = build_large_python_fixture()


def post_json(path: str, body: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url=f"{BASE_URL}{path}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with NO_PROXY_OPENER.open(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{path} returned HTTP {exc.code}: {detail}") from exc


def normalize_tool_calls(tool_calls: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in tool_calls or []:
        function = item.get("function", {})
        name = function.get("name")
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                pass
        if (
            name == "write_file"
            and isinstance(arguments, dict)
            and isinstance(arguments.get("content"), str)
            and arguments["content"].endswith("\n")
            and not arguments["content"].endswith("\n\n")
        ):
            arguments = dict(arguments)
            arguments["content"] = arguments["content"][:-1]
        normalized.append(
            {
                "name": name,
                "arguments": arguments,
            }
        )
    return normalized


def assert_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise AssertionError(
            f"{label} mismatch\nexpected: {json.dumps(expected, ensure_ascii=False, indent=2)}\n"
            f"actual:   {json.dumps(actual, ensure_ascii=False, indent=2)}"
        )


def format_debug_payload(debug_payload: dict[str, Any]) -> str:
    return json.dumps(
        {
            "content": debug_payload.get("content", ""),
            "tool_calls": debug_payload.get("tool_calls", []),
            "raw_text": debug_payload.get("raw_text", ""),
        },
        ensure_ascii=False,
        indent=2,
    )


@dataclass(frozen=True)
class Case:
    name: str
    request_body: dict[str, Any]
    expected_content: str
    expected_tool_calls: list[dict[str, Any]]
    expected_finish_reason: str


CASES = [
    Case(
        name="exact_text_ascii",
        request_body={
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Reply with exactly the following text and nothing else.\n"
                        "Do not add quotes, markdown, explanation, or extra whitespace.\n"
                        "ECHO_CASE_001|alpha|42"
                    ),
                }
            ],
        },
        expected_content="ECHO_CASE_001|alpha|42",
        expected_tool_calls=[],
        expected_finish_reason="stop",
    ),
    Case(
        name="exact_text_chinese",
        request_body={
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "请逐字逐符号输出下面这一整行，任何字符都不能增删改，不能加引号：\n"
                        "CN_EXACT_003|固定输出样例003：今天天气测试正常。"
                    ),
                }
            ],
        },
        expected_content="CN_EXACT_003|固定输出样例003：今天天气测试正常。",
        expected_tool_calls=[],
        expected_finish_reason="stop",
    ),
    Case(
        name="exact_text_multiline_special_chars",
        request_body={
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Reply with exactly the following 3 lines and nothing else.\n"
                        "Line 1: CASE_002\n"
                        'Line 2: {"k":"v","n":7}\n'
                        "Line 3: 中文，done.\n"
                        "Do not wrap in code fences."
                    ),
                }
            ],
        },
        expected_content='CASE_002\n{"k":"v","n":7}\n中文，done.',
        expected_tool_calls=[],
        expected_finish_reason="stop",
    ),
    Case(
        name="single_write_file_tool_call",
        request_body={
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Use exactly one write_file tool call and no other tool calls.\n"
                        "Set path to /tmp/provider_case_003.txt\n"
                        "Set content to EXACT_CASE_003\n"
                        "Do not add any assistant text outside the tool call."
                    ),
                }
            ],
            "tools": [WRITE_FILE_TOOL],
        },
        expected_content="",
        expected_tool_calls=[
            {
                "name": "write_file",
                "arguments": {
                    "path": "/tmp/provider_case_003.txt",
                    "content": "EXACT_CASE_003",
                },
            }
        ],
        expected_finish_reason="tool_calls",
    ),
    Case(
        name="single_bash_tool_call_with_quotes",
        request_body={
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Use exactly one bash tool call and no assistant text.\n"
                        "Set description to CASE_005_BASH.\n"
                        'Set command to: python -c "print(\\"CASE_005\\")"'
                    ),
                }
            ],
            "tools": [BASH_TOOL],
        },
        expected_content="",
        expected_tool_calls=[
            {
                "name": "bash",
                "arguments": {
                    "description": "CASE_005_BASH",
                    'command': 'python -c "print(\\"CASE_005\\")"',
                },
            }
        ],
        expected_finish_reason="tool_calls",
    ),
    Case(
        name="two_tool_calls_fixed_args",
        request_body={
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Use exactly two tool calls in this order and do not add assistant text.\n"
                        "First call bash with description CHECK_PWD and command pwd.\n"
                        "Second call write_file with path /tmp/provider_case_004.txt and content CASE_004."
                    ),
                }
            ],
            "tools": [BASH_TOOL, WRITE_FILE_TOOL],
        },
        expected_content="",
        expected_tool_calls=[
            {
                "name": "bash",
                "arguments": {
                    "description": "CHECK_PWD",
                    "command": "pwd",
                },
            },
            {
                "name": "write_file",
                "arguments": {
                    "path": "/tmp/provider_case_004.txt",
                    "content": "CASE_004",
                },
            },
        ],
        expected_finish_reason="tool_calls",
    ),
    Case(
        name="write_file_tool_call_with_python_content",
        request_body={
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Use exactly one write_file tool call.\n"
                        "Set path to /tmp/provider_case_006.py.\n"
                        "Set content exactly to these two lines:\n"
                        "print(\"CASE_006\")\n"
                        "print(\"done\")\n"
                        "Do not add assistant text."
                    ),
                }
            ],
            "tools": [WRITE_FILE_TOOL],
        },
        expected_content="",
        expected_tool_calls=[
            {
                "name": "write_file",
                "arguments": {
                    "path": "/tmp/provider_case_006.py",
                    'content': 'print("CASE_006")\nprint("done")',
                },
            }
        ],
        expected_finish_reason="tool_calls",
    ),
    Case(
        name="content_plus_single_tool_call",
        request_body={
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "First set assistant content exactly to PRELUDE_CASE_007.\n"
                        "Also include exactly one write_file tool call.\n"
                        "Use path /tmp/provider_case_007.txt and content CASE_007.\n"
                        "Do not add any other text."
                    ),
                }
            ],
            "tools": [WRITE_FILE_TOOL],
        },
        expected_content="PRELUDE_CASE_007",
        expected_tool_calls=[
            {
                "name": "write_file",
                "arguments": {
                    "path": "/tmp/provider_case_007.txt",
                    "content": "CASE_007",
                },
            }
        ],
        expected_finish_reason="tool_calls",
    ),
    Case(
        name="large_multiline_text_100_plus_lines",
        request_body={
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Reply with exactly the following text and nothing else.\n"
                        "Do not use code fences.\n"
                        f"{LARGE_PYTHON_FIXTURE}"
                    ),
                }
            ],
        },
        expected_content=LARGE_PYTHON_FIXTURE,
        expected_tool_calls=[],
        expected_finish_reason="stop",
    ),
    Case(
        name="large_write_file_tool_call_100_plus_lines",
        request_body={
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Use exactly one write_file tool call.\n"
                        "Do not add assistant text.\n"
                        "Set path to /tmp/provider_case_100.py.\n"
                        "Set content exactly to the following code:\n"
                        f"{LARGE_PYTHON_FIXTURE}"
                    ),
                }
            ],
            "tools": [WRITE_FILE_TOOL],
        },
        expected_content="",
        expected_tool_calls=[
            {
                "name": "write_file",
                "arguments": {
                    "path": "/tmp/provider_case_100.py",
                    "content": LARGE_PYTHON_FIXTURE,
                },
            }
        ],
        expected_finish_reason="tool_calls",
    ),
]


def run_case(case: Case) -> None:
    print(f"[RUN] {case.name} -> /v1/chat/completions", flush=True)
    completion = post_json("/v1/chat/completions", case.request_body)
    print(f"[RUN] {case.name} -> /debug/chat-timings", flush=True)
    debug_response = post_json(
        "/debug/chat-timings",
        {
            **case.request_body,
            "include_payload": True,
        },
    )
    print(f"[RUN] {case.name} -> validating", flush=True)
    validate_case(case, completion, debug_response)


def validate_case(case: Case, completion: dict[str, Any], debug_response: dict[str, Any]) -> None:
    choice = completion["choices"][0]
    message = choice["message"]
    actual_content = message.get("content", "")
    actual_finish_reason = choice.get("finish_reason")
    actual_tool_calls = normalize_tool_calls(message.get("tool_calls"))
    debug_payload = debug_response.get("payload", {})

    try:
        assert_equal(actual_content, case.expected_content, f"{case.name} content")
        assert_equal(actual_finish_reason, case.expected_finish_reason, f"{case.name} finish_reason")
        assert_equal(actual_tool_calls, case.expected_tool_calls, f"{case.name} tool_calls")
    except AssertionError as exc:
        raise AssertionError(f"{exc}\nprovider_payload:\n{format_debug_payload(debug_payload)}") from exc

    provider_content = debug_payload.get("content", "")
    provider_tool_calls = debug_payload.get("tool_calls", [])

    print(f"[PASS] {case.name}")
    print(f"  finish_reason: {actual_finish_reason}")
    print(f"  content: {json.dumps(actual_content, ensure_ascii=False)}")
    print(f"  tool_calls: {json.dumps(actual_tool_calls, ensure_ascii=False)}")
    print(f"  provider_content: {json.dumps(provider_content, ensure_ascii=False)}")
    print(f"  provider_tool_calls: {json.dumps(provider_tool_calls, ensure_ascii=False)}")


def run_case_v1_only(case: Case) -> None:
    print(f"[RUN] {case.name} -> /v1/chat/completions", flush=True)
    completion = post_json("/v1/chat/completions", case.request_body)
    body_size = len(json.dumps(completion, ensure_ascii=False))
    print(f"[RUN] {case.name} -> /v1 complete, response_chars={body_size}", flush=True)
    choice = completion["choices"][0]
    message = choice["message"]
    actual_content = message.get("content", "")
    actual_finish_reason = choice.get("finish_reason")
    actual_tool_calls = normalize_tool_calls(message.get("tool_calls"))

    assert_equal(actual_content, case.expected_content, f"{case.name} content")
    assert_equal(actual_finish_reason, case.expected_finish_reason, f"{case.name} finish_reason")
    assert_equal(actual_tool_calls, case.expected_tool_calls, f"{case.name} tool_calls")

    print(f"[PASS] {case.name} (/v1 only)")
    print(f"  finish_reason: {actual_finish_reason}")
    print(f"  content_chars: {len(actual_content)}")
    print(f"  tool_call_count: {len(actual_tool_calls)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare fixed-output DeepSeek provider cases.")
    parser.add_argument("--case", action="append", help="Run only the named case. Can be passed multiple times.")
    parser.add_argument("--skip-debug", action="store_true", help="Run only /v1/chat/completions and skip /debug/chat-timings.")
    args = parser.parse_args()

    selected = [case for case in CASES if not args.case or case.name in args.case]
    if not selected:
        available = ", ".join(case.name for case in CASES)
        print(f"No matching cases. Available: {available}", file=sys.stderr)
        return 2

    failed = False
    for case in selected:
        try:
            if args.skip_debug:
                run_case_v1_only(case)
            else:
                run_case(case)
        except Exception as exc:
            failed = True
            print(f"[FAIL] {case.name}: {exc}", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
