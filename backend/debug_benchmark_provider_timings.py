from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass
class RunSample:
    model: str
    run_index: int
    wall_ms: int
    internal_total_ms: int
    steps_ms: dict[str, int]
    extra_ms: dict[str, int]


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if q <= 0:
        return min(values)
    if q >= 1:
        return max(values)
    sorted_values = sorted(values)
    idx = q * (len(sorted_values) - 1)
    low = math.floor(idx)
    high = math.ceil(idx)
    if low == high:
        return float(sorted_values[low])
    weight = idx - low
    return float(sorted_values[low] * (1 - weight) + sorted_values[high] * weight)


def post_json(base_url: str, path: str, body: dict[str, Any], timeout_sec: int) -> dict[str, Any]:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url=f"{base_url.rstrip('/')}{path}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with NO_PROXY_OPENER.open(request, timeout=timeout_sec) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{path} returned HTTP {exc.code}: {detail}") from exc


def run_single_sample(
    *,
    base_url: str,
    model: str,
    prompt: str,
    timeout_sec: int,
    run_index: int,
) -> RunSample:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [],
        "include_payload": False,
    }

    started = time.perf_counter()
    response = post_json(base_url, "/debug/chat-timings", body, timeout_sec=timeout_sec)
    wall_ms = int((time.perf_counter() - started) * 1000)

    timing = response.get("timing", {})
    steps_ms = timing.get("steps_ms", {}) if isinstance(timing, dict) else {}
    total_ms = timing.get("total_ms", 0) if isinstance(timing, dict) else 0

    if not isinstance(steps_ms, dict):
        steps_ms = {}
    normalized_steps = {str(k): int(v) for k, v in steps_ms.items() if isinstance(v, int | float)}
    extra_ms: dict[str, int] = {}
    if isinstance(timing, dict):
        for key, value in timing.items():
            if key in {"steps_ms", "total_ms"}:
                continue
            if isinstance(value, int | float) and str(key).endswith("_ms"):
                extra_ms[str(key)] = int(value)

    return RunSample(
        model=model,
        run_index=run_index,
        wall_ms=wall_ms,
        internal_total_ms=int(total_ms) if isinstance(total_ms, int | float) else 0,
        steps_ms=normalized_steps,
        extra_ms=extra_ms,
    )


def summarize_metric(values: list[int]) -> dict[str, float]:
    if not values:
        return {"min": 0.0, "p50": 0.0, "p95": 0.0, "mean": 0.0, "max": 0.0}
    numeric = [float(v) for v in values]
    return {
        "min": min(numeric),
        "p50": percentile(numeric, 0.50),
        "p95": percentile(numeric, 0.95),
        "mean": statistics.fmean(numeric),
        "max": max(numeric),
    }


def print_summary(samples: list[RunSample], model: str) -> None:
    model_samples = [s for s in samples if s.model == model]
    if not model_samples:
        print(f"\n=== {model} ===")
        print("No samples.")
        return

    wall_stats = summarize_metric([s.wall_ms for s in model_samples])
    internal_stats = summarize_metric([s.internal_total_ms for s in model_samples])
    overhead_values = [max(0, s.wall_ms - s.internal_total_ms) for s in model_samples]
    overhead_stats = summarize_metric(overhead_values)

    print(f"\n=== {model} ({len(model_samples)} runs) ===")
    print(
        "Wall(ms):    min={min:.0f} p50={p50:.0f} p95={p95:.0f} mean={mean:.0f} max={max:.0f}".format(
            **wall_stats
        )
    )
    print(
        "Internal(ms):min={min:.0f} p50={p50:.0f} p95={p95:.0f} mean={mean:.0f} max={max:.0f}".format(
            **internal_stats
        )
    )
    print(
        "Overhead(ms):min={min:.0f} p50={p50:.0f} p95={p95:.0f} mean={mean:.0f} max={max:.0f}".format(
            **overhead_stats
        )
    )

    all_step_names: set[str] = set()
    for sample in model_samples:
        all_step_names.update(sample.steps_ms.keys())

    if not all_step_names:
        print("No internal step metrics returned.")
    else:
        print("Top slow steps by p50:")
        ranked: list[tuple[str, dict[str, float]]] = []
        for step_name in sorted(all_step_names):
            stats = summarize_metric([s.steps_ms.get(step_name, 0) for s in model_samples])
            ranked.append((step_name, stats))
        ranked.sort(key=lambda item: item[1]["p50"], reverse=True)

        for step_name, stats in ranked[:10]:
            print(
                "  {name}: p50={p50:.0f}ms p95={p95:.0f}ms mean={mean:.0f}ms".format(
                    name=step_name,
                    p50=stats["p50"],
                    p95=stats["p95"],
                    mean=stats["mean"],
                )
            )

    all_extra_names: set[str] = set()
    for sample in model_samples:
        all_extra_names.update(sample.extra_ms.keys())

    if all_extra_names:
        print("Additional timing fields:")
        extra_ranked: list[tuple[str, dict[str, float]]] = []
        for name in sorted(all_extra_names):
            stats = summarize_metric([s.extra_ms.get(name, 0) for s in model_samples])
            extra_ranked.append((name, stats))
        extra_ranked.sort(key=lambda item: item[1]["p50"], reverse=True)
        for name, stats in extra_ranked:
            print(
                "  {name}: p50={p50:.0f}ms p95={p95:.0f}ms mean={mean:.0f}ms".format(
                    name=name,
                    p50=stats["p50"],
                    p95=stats["p95"],
                    mean=stats["mean"],
                )
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark DeepSeek local provider timing stages.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765", help="Provider base URL.")
    parser.add_argument(
        "--models",
        nargs="+",
        default=["DeepSeekV4", "DeepSeekV4-thinking"],
        help="Model names to benchmark.",
    )
    parser.add_argument("--runs", type=int, default=5, help="Runs per model.")
    parser.add_argument("--timeout-sec", type=int, default=180, help="HTTP timeout per run.")
    parser.add_argument(
        "--prompt",
        default="Reply with exactly: perf_probe_ok",
        help="Simple prompt used for timing probe.",
    )
    parser.add_argument("--output-json", default="", help="Optional path to save raw benchmark JSON.")
    args = parser.parse_args()

    samples: list[RunSample] = []
    for model in args.models:
        print(f"\nRunning model: {model}")
        for i in range(1, args.runs + 1):
            try:
                sample = run_single_sample(
                    base_url=args.base_url,
                    model=model,
                    prompt=args.prompt,
                    timeout_sec=args.timeout_sec,
                    run_index=i,
                )
                samples.append(sample)
                print(
                    f"  run {i}/{args.runs}: wall={sample.wall_ms}ms internal={sample.internal_total_ms}ms steps={len(sample.steps_ms)}"
                )
            except Exception as exc:
                print(f"  run {i}/{args.runs}: ERROR: {exc}")

    for model in args.models:
        print_summary(samples, model)

    if args.output_json:
        payload = {
            "base_url": args.base_url,
            "models": args.models,
            "runs": args.runs,
            "prompt": args.prompt,
            "samples": [
                {
                    "model": s.model,
                    "run_index": s.run_index,
                    "wall_ms": s.wall_ms,
                    "internal_total_ms": s.internal_total_ms,
                    "steps_ms": s.steps_ms,
                    "extra_ms": s.extra_ms,
                }
                for s in samples
            ],
        }
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\nSaved raw benchmark to: {args.output_json}")


if __name__ == "__main__":
    main()
