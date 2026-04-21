from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HEALTHCHECK_PATH = "/health"
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
MODEL_NAME = "xiaomi-mimo-v2-pro"
ACQUIRE_RE = re.compile(r"acquired bridge slot=(\d+)")
RELEASE_RE = re.compile(r"released bridge slot=(\d+)")


@dataclass(frozen=True)
class ProbeCase:
    pool_size: int
    round_index: int
    request_index: int
    expected_text: str
    prompt: str
    user: str


@dataclass
class ProbeResult:
    pool_size: int
    round_index: int
    request_index: int
    expected_text: str
    status_code: int | None = None
    finish_reason: str | None = None
    content: str | None = None
    wall_ms: int | None = None
    error: str | None = None
    raw_preview: str | None = None
    failure_reasons: list[str] = field(default_factory=list)

    @property
    def exact_match(self) -> bool:
        return self.error is None and self.status_code == 200 and self.content == self.expected_text


@dataclass
class RoundSummary:
    pool_size: int
    round_index: int
    request_count: int
    passed: bool
    exact_match_count: int
    unique_actual_count: int
    wall_ms_max: int
    wall_ms_p50: int
    results: list[ProbeResult]


@dataclass
class ProviderLogSummary:
    used_slots: list[int]
    acquire_count: int
    release_count: int
    max_inflight: int


@dataclass
class PoolSummary:
    pool_size: int
    port: int
    model: str
    request_count: int
    rounds: int
    passed: bool
    round_summaries: list[RoundSummary]
    provider_log: ProviderLogSummary
    provider_log_path: str


def json_preview(value: Any, *, max_chars: int = 800) -> str:
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}...<truncated>"


def percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    if q <= 0:
        return min(values)
    if q >= 1:
        return max(values)
    ordered = sorted(values)
    index = q * (len(ordered) - 1)
    low = int(index)
    high = min(len(ordered) - 1, low + 1)
    weight = index - low
    return int(round(ordered[low] * (1 - weight) + ordered[high] * weight))


def random_token(length: int = 10) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def make_probe_case(pool_size: int, round_index: int, request_index: int) -> ProbeCase:
    token = random_token()
    expected_text = (
        f"MIMO_CONCURRENCY|POOL={pool_size}|ROUND={round_index}|REQ={request_index}|TOKEN={token}|END"
    )
    prompt = (
        "你在做 provider 并发隔离测试。\n"
        "请严格只输出下一行文本，不要添加任何别的字符、空格、引号、代码块、解释或前后缀。\n"
        f"{expected_text}"
    )
    user = f"mimo-concurrency-p{pool_size}-r{round_index}-q{request_index}-{token.lower()}"
    return ProbeCase(
        pool_size=pool_size,
        round_index=round_index,
        request_index=request_index,
        expected_text=expected_text,
        prompt=prompt,
        user=user,
    )


def build_provider_command(*, host: str, port: int) -> list[str]:
    uv_binary = shutil.which("uv")
    if uv_binary:
        return [
            uv_binary,
            "run",
            "uvicorn",
            "app.deepseek_local_provider:app",
            "--host",
            host,
            "--port",
            str(port),
            "--log-level",
            "warning",
        ]
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "app.deepseek_local_provider:app",
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]


def tail_text(path: Path, *, max_chars: int = 4000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def wait_for_health(base_url: str, *, timeout_sec: int) -> None:
    deadline = time.monotonic() + timeout_sec
    last_error = ""
    while time.monotonic() < deadline:
        request = urllib.request.Request(f"{base_url}{HEALTHCHECK_PATH}", method="GET")
        try:
            with NO_PROXY_OPENER.open(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if response.status == 200 and payload.get("status") == "ok":
                return
            last_error = f"unexpected health payload: {payload}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"provider health check timed out after {timeout_sec}s: {last_error}")


def post_json(base_url: str, path: str, body: dict[str, Any], *, timeout_sec: int) -> tuple[int, dict[str, Any]]:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url=f"{base_url}{path}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with NO_PROXY_OPENER.open(request, timeout=timeout_sec) as response:
            data = json.loads(response.read().decode("utf-8"))
            return response.status, data
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def run_single_probe(base_url: str, case: ProbeCase, *, timeout_sec: int, start_barrier: threading.Barrier) -> ProbeResult:
    result = ProbeResult(
        pool_size=case.pool_size,
        round_index=case.round_index,
        request_index=case.request_index,
        expected_text=case.expected_text,
    )
    try:
        start_barrier.wait(timeout=30)
    except threading.BrokenBarrierError as exc:
        result.error = f"barrier_failed: {exc}"
        return result

    body = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": case.prompt}],
        "stream": False,
        "user": case.user,
    }

    started = time.perf_counter()
    try:
        status_code, response_body = post_json(
            base_url,
            CHAT_COMPLETIONS_PATH,
            body,
            timeout_sec=timeout_sec,
        )
        result.status_code = status_code
        result.raw_preview = json_preview(response_body)
        choices = response_body.get("choices")
        if not isinstance(choices, list) or not choices:
            result.error = "missing_choices"
            return result
        choice0 = choices[0] if isinstance(choices[0], dict) else {}
        message = choice0.get("message", {})
        if not isinstance(message, dict):
            result.error = "missing_message"
            return result
        content = message.get("content")
        result.content = content if isinstance(content, str) else None if content is None else str(content)
        finish_reason = choice0.get("finish_reason")
        result.finish_reason = finish_reason if isinstance(finish_reason, str) else str(finish_reason)
    except Exception as exc:
        result.error = str(exc)
    finally:
        result.wall_ms = int((time.perf_counter() - started) * 1000)
    return result


def annotate_failures(results: list[ProbeResult]) -> None:
    actual_counter = Counter(result.content for result in results if isinstance(result.content, str))
    expected_tokens = [result.expected_text for result in results]
    for result in results:
        reasons: list[str] = []
        if result.error:
            reasons.append(f"request_error:{result.error}")
        if result.status_code != 200:
            reasons.append(f"http_status:{result.status_code}")
        if result.finish_reason != "stop":
            reasons.append(f"finish_reason:{result.finish_reason}")
        if result.content != result.expected_text:
            reasons.append("content_mismatch")
        if result.content and actual_counter[result.content] > 1:
            reasons.append("duplicate_actual_output")

        if result.content:
            leaked = [
                token
                for token in expected_tokens
                if token != result.expected_text and token in result.content
            ]
            if leaked:
                reasons.append(f"cross_case_token_leak:{len(leaked)}")

        result.failure_reasons = reasons


def summarize_round(pool_size: int, round_index: int, results: list[ProbeResult]) -> RoundSummary:
    annotate_failures(results)
    actual_unique = {result.content for result in results if isinstance(result.content, str)}
    wall_values = [result.wall_ms or 0 for result in results]
    exact_match_count = sum(1 for result in results if result.exact_match)
    passed = exact_match_count == len(results) and len(actual_unique) == len(results)
    return RoundSummary(
        pool_size=pool_size,
        round_index=round_index,
        request_count=len(results),
        passed=passed,
        exact_match_count=exact_match_count,
        unique_actual_count=len(actual_unique),
        wall_ms_max=max(wall_values) if wall_values else 0,
        wall_ms_p50=percentile(wall_values, 0.50),
        results=results,
    )


def parse_provider_log(log_path: Path) -> ProviderLogSummary:
    used_slots: Counter[int] = Counter()
    inflight = 0
    max_inflight = 0
    acquire_count = 0
    release_count = 0

    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            acquire_match = ACQUIRE_RE.search(line)
            if acquire_match:
                slot = int(acquire_match.group(1))
                used_slots[slot] += 1
                acquire_count += 1
                inflight += 1
                max_inflight = max(max_inflight, inflight)
                continue

            release_match = RELEASE_RE.search(line)
            if release_match:
                release_count += 1
                inflight = max(0, inflight - 1)

    return ProviderLogSummary(
        used_slots=sorted(used_slots),
        acquire_count=acquire_count,
        release_count=release_count,
        max_inflight=max_inflight,
    )


class ProviderProcess:
    def __init__(
        self,
        *,
        backend_dir: Path,
        host: str,
        port: int,
        pool_size: int,
        log_path: Path,
        startup_timeout_sec: int,
        headless: bool,
    ) -> None:
        self.backend_dir = backend_dir
        self.host = host
        self.port = port
        self.pool_size = pool_size
        self.log_path = log_path
        self.startup_timeout_sec = startup_timeout_sec
        self.headless = headless
        self.process: subprocess.Popen[str] | None = None
        self._log_handle = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("w", encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["DEEPSEEK_WEB_POOL_SIZE"] = str(self.pool_size)
        env["XIAOMI_MIMO_WEB_HEADLESS"] = "1" if self.headless else "0"
        command = build_provider_command(host=self.host, port=self.port)
        self.process = subprocess.Popen(
            command,
            cwd=self.backend_dir,
            env=env,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            wait_for_health(self.base_url, timeout_sec=self.startup_timeout_sec)
        except Exception:
            self.stop()
            log_tail = tail_text(self.log_path)
            raise RuntimeError(
                f"provider failed to start on {self.base_url}\nlog tail:\n{log_tail}"
            ) from None

    def stop(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        self.process = None

    def __enter__(self) -> "ProviderProcess":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


def run_round(
    *,
    base_url: str,
    pool_size: int,
    round_index: int,
    request_count: int,
    timeout_sec: int,
) -> RoundSummary:
    cases = [make_probe_case(pool_size, round_index, request_index) for request_index in range(1, request_count + 1)]
    start_barrier = threading.Barrier(len(cases))
    results: list[ProbeResult] = [ProbeResult(pool_size, round_index, case.request_index, case.expected_text) for case in cases]

    def worker(slot_index: int, case: ProbeCase) -> None:
        results[slot_index] = run_single_probe(
            base_url,
            case,
            timeout_sec=timeout_sec,
            start_barrier=start_barrier,
        )

    threads: list[threading.Thread] = []
    for slot_index, case in enumerate(cases):
        thread = threading.Thread(
            target=worker,
            args=(slot_index, case),
            name=f"mimo-probe-p{pool_size}-r{round_index}-q{case.request_index}",
        )
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()

    return summarize_round(pool_size, round_index, results)


def run_pool_case(
    *,
    backend_dir: Path,
    pool_size: int,
    port: int,
    request_count: int,
    rounds: int,
    startup_timeout_sec: int,
    request_timeout_sec: int,
    log_dir: Path,
    headless: bool,
) -> PoolSummary:
    log_path = log_dir / f"mimo-provider-pool-{pool_size}.log"
    round_summaries: list[RoundSummary] = []

    with ProviderProcess(
        backend_dir=backend_dir,
        host="127.0.0.1",
        port=port,
        pool_size=pool_size,
        log_path=log_path,
        startup_timeout_sec=startup_timeout_sec,
        headless=headless,
    ) as provider:
        for round_index in range(1, rounds + 1):
            round_summary = run_round(
                base_url=provider.base_url,
                pool_size=pool_size,
                round_index=round_index,
                request_count=request_count,
                timeout_sec=request_timeout_sec,
            )
            round_summaries.append(round_summary)

    provider_log = parse_provider_log(log_path)
    passed = all(round_summary.passed for round_summary in round_summaries)
    return PoolSummary(
        pool_size=pool_size,
        port=port,
        model=MODEL_NAME,
        request_count=request_count,
        rounds=rounds,
        passed=passed,
        round_summaries=round_summaries,
        provider_log=provider_log,
        provider_log_path=str(log_path),
    )


def print_round_summary(summary: RoundSummary) -> None:
    status = "PASS" if summary.passed else "FAIL"
    print(
        f"  round {summary.round_index}: {status} "
        f"exact={summary.exact_match_count}/{summary.request_count} "
        f"unique_actual={summary.unique_actual_count}/{summary.request_count} "
        f"wall_p50={summary.wall_ms_p50}ms wall_max={summary.wall_ms_max}ms"
    )
    if summary.passed:
        return
    for result in summary.results:
        if not result.failure_reasons:
            continue
        print(
            "    req {req}: reasons={reasons} expected={expected} actual={actual}".format(
                req=result.request_index,
                reasons=",".join(result.failure_reasons),
                expected=result.expected_text,
                actual=result.content if result.content is not None else result.raw_preview,
            )
        )


def print_pool_summary(summary: PoolSummary) -> None:
    status = "PASS" if summary.passed else "FAIL"
    print(f"\n=== pool_size={summary.pool_size} ({status}) ===")
    print(f"port: {summary.port}")
    print(f"model: {summary.model}")
    print(f"log: {summary.provider_log_path}")
    for round_summary in summary.round_summaries:
        print_round_summary(round_summary)
    print(
        "provider_log: used_slots={slots} acquire_count={acquire} release_count={release} max_inflight={max_inflight}".format(
            slots=summary.provider_log.used_slots,
            acquire=summary.provider_log.acquire_count,
            release=summary.provider_log.release_count,
            max_inflight=summary.provider_log.max_inflight,
        )
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe Xiaomi MiMo provider concurrency isolation for pool sizes 2/3.")
    parser.add_argument(
        "--pool-sizes",
        nargs="+",
        type=int,
        default=[2, 3],
        help="Provider-side DEEPSEEK_WEB_POOL_SIZE values to test.",
    )
    parser.add_argument(
        "--requests-per-round",
        type=int,
        default=0,
        help="Concurrent requests to fire in each round. Default: use current pool size.",
    )
    parser.add_argument("--rounds", type=int, default=2, help="Rounds per pool-size case.")
    parser.add_argument(
        "--start-port",
        type=int,
        default=8872,
        help="First provider port. Later pool-size cases use sequential ports.",
    )
    parser.add_argument(
        "--startup-timeout-sec",
        type=int,
        default=60,
        help="Provider startup health-check timeout.",
    )
    parser.add_argument(
        "--request-timeout-sec",
        type=int,
        default=240,
        help="Timeout for each MiMo request.",
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Run provider with visible Xiaomi MiMo browser instead of headless mode.",
    )
    parser.add_argument(
        "--log-dir",
        default=str(Path("/tmp/mimo-provider-concurrency")),
        help="Directory for provider logs and optional JSON output.",
    )
    parser.add_argument(
        "--output-json",
        default="",
        help="Optional path to save the structured summary as JSON.",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    backend_dir = Path(__file__).resolve().parent
    log_dir = Path(args.log_dir).expanduser()
    summaries: list[PoolSummary] = []

    for offset, pool_size in enumerate(args.pool_sizes):
        if pool_size < 1:
            raise ValueError(f"invalid pool size: {pool_size}")
        request_count = args.requests_per_round if args.requests_per_round > 0 else pool_size
        port = args.start_port + offset
        print(
            f"\nStarting pool_size={pool_size} on http://127.0.0.1:{port} "
            f"with request_count={request_count}, rounds={args.rounds}"
        )
        summary = run_pool_case(
            backend_dir=backend_dir,
            pool_size=pool_size,
            port=port,
            request_count=request_count,
            rounds=args.rounds,
            startup_timeout_sec=args.startup_timeout_sec,
            request_timeout_sec=args.request_timeout_sec,
            log_dir=log_dir,
            headless=not args.show_browser,
        )
        summaries.append(summary)
        print_pool_summary(summary)

    overall_passed = all(summary.passed for summary in summaries)
    print(f"\nOVERALL: {'PASS' if overall_passed else 'FAIL'}")

    if args.output_json:
        output_path = Path(args.output_json).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model": MODEL_NAME,
            "pool_sizes": args.pool_sizes,
            "requests_per_round": args.requests_per_round,
            "rounds": args.rounds,
            "overall_passed": overall_passed,
            "summaries": [asdict(summary) for summary in summaries],
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved_json: {output_path}")

    return 0 if overall_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
