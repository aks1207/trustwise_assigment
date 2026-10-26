"""Run judges over the saved sample. Every run gets a new folder; nothing is ever overwritten.

    python -m experiment.run --models luna haiku --repeats 3
    python -m experiment.run --models luna --repeats 1 --limit 5 --batch smoke

Layout:
    runs/<batch>/<run_id>/meta.json        written at start (policy hash, model, params, sample, seed, ...)
    runs/<batch>/<run_id>/results.jsonl    one line per item: ids, stratum, label, and the engine's safe result
    runs/<batch>/<run_id>/audit.jsonl      one audit record per item: content, exact prompts, raw replies
    runs/<batch>/<run_id>/completed.json   written only when the run finishes; a run without it is incomplete

All files are created with mode "x" (fail if present) or appended. Only `text` and `mode` reach the engine;
label, category and stratum are attached to the saved line afterwards, for scoring.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from policy_engine import ENGINE_VERSION, AuditLog, Engine, load_policy
from policy_engine.judge import Judge
from policy_engine.litellm_judge import PRESETS, LiteLLMJudge

from .dataset import ROOT, dataset_sha256, load_rows

DEFAULT_POLICY = ROOT / "policy" / "content_safety_policy.yaml"
DEFAULT_SAMPLE = ROOT / "runs" / "sample_seed20.json"
RUNS = ROOT / "runs"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_new(path: Path, obj: Any) -> None:
    with open(path, "x", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def load_sample(path: Path) -> dict:
    sample = json.loads(path.read_text())
    if sample["dataset_sha256"] != dataset_sha256():
        raise SystemExit(f"{path}: dataset hash does not match sanity_subset.csv; the sample is not for this file")
    return sample


async def run_once(
    *,
    judge: Judge,
    model_name: str,
    repeat: int,
    batch: str,
    policy_path: Path,
    sample_path: Path,
    item_concurrency: int,
    limit: int | None = None,
    runs_dir: Path = RUNS,
    engine_factory: Callable[..., Engine] = Engine,
    progress: bool = True,
) -> Path:
    policy = load_policy(policy_path)
    sample = load_sample(sample_path)
    items = sample["items"][:limit] if limit else sample["items"]
    texts = {r.id: r for r in load_rows()}

    started = _now()
    run_id = f"{started.strftime('%Y%m%dT%H%M%SZ')}-{model_name}-r{repeat}"
    sample_sha = hashlib.sha256(sample_path.read_bytes()).hexdigest()
    for other in (runs_dir / batch).glob("*/meta.json"):
        if json.loads(other.read_text())["sample"]["sha256"] != sample_sha:
            raise SystemExit(f"batch {batch} already holds runs on a different sample; use another --batch")
    run_dir = runs_dir / batch / run_id
    run_dir.mkdir(parents=True, exist_ok=False)          # a second run in the same second fails loudly

    _write_new(run_dir / "meta.json", {
        "run_id": run_id,
        "batch": batch,
        "repeat": repeat,
        "model_name": model_name,
        "judge_model": judge.model,
        "judge_params": judge.params(),
        "execution_kind": judge.execution_kind,
        "policy": {"path": str(policy_path.relative_to(ROOT)) if policy_path.is_relative_to(ROOT) else str(policy_path),
                   "name": policy.name, "version": policy.version, "sha256": policy.sha256,
                   "evaluation_strategy": policy.evaluation_strategy, "threshold": policy.threshold},
        "sample": {"path": str(sample_path.relative_to(ROOT)) if sample_path.is_relative_to(ROOT) else str(sample_path),
                   "sha256": hashlib.sha256(sample_path.read_bytes()).hexdigest(),
                   "seed": sample["seed"], "size": len(items), "limit": limit},
        "dataset_sha256": sample["dataset_sha256"],
        "fields_sent_to_judge": ["text", "mode"],
        "engine_version": ENGINE_VERSION,
        "item_concurrency": item_concurrency,
        "python": platform.python_version(),
        "started_at": _iso(started),
    })

    audit = AuditLog(run_dir / "audit.jsonl")
    engine = engine_factory(policy, judge, audit_sink=audit)
    results_path = run_dir / "results.jsonl"
    results_path.touch(exist_ok=False)
    gate = asyncio.Semaphore(item_concurrency)
    done = {"ok": 0, "error": 0}
    t0 = time.perf_counter()

    async def one(item: dict) -> None:
        row = texts[item["id"]]
        line: dict[str, Any] = {"item_id": item["id"], "stratum": item["stratum"], "category": item["category"],
                                "mode": item["mode"], "label": item["label"]}
        async with gate:
            try:
                out = await engine.evaluate(row.text, row.mode)
                line["result"] = out.result
                done["ok"] += 1
            except Exception as exc:  # kept, never dropped: the item counts as not evaluated
                line["error"] = f"{type(exc).__name__}: {exc}"
                done["error"] += 1
        with open(results_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        n = done["ok"] + done["error"]
        if progress and (n % 10 == 0 or n == len(items)):
            print(f"  {run_id}: {n}/{len(items)} ({time.perf_counter() - t0:.0f}s)", flush=True)

    await asyncio.gather(*(one(i) for i in items))

    lines = [json.loads(l) for l in results_path.read_text().splitlines() if l.strip()]
    totals = [l["result"]["totals"] for l in lines if "result" in l]
    cost = [t["cost_usd"] for t in totals]
    _write_new(run_dir / "completed.json", {
        "run_id": run_id,
        "finished_at": _iso(_now()),
        "wall_clock_s": round(time.perf_counter() - t0, 1),
        "items": len(items),
        "items_ok": done["ok"],
        "items_error": done["error"],
        "judge_attempts": sum(t["attempt_count"] for t in totals),
        "input_tokens": None if any(t["input_tokens"] is None for t in totals) else sum(t["input_tokens"] for t in totals),
        "output_tokens": None if any(t["output_tokens"] is None for t in totals) else sum(t["output_tokens"] for t in totals),
        "cost_usd": None if any(c is None for c in cost) else round(sum(cost), 6),
    })
    return run_dir


async def _main(args: argparse.Namespace) -> int:
    policy_path = Path(args.policy).resolve()
    sample_path = Path(args.sample).resolve()
    seed = json.loads(sample_path.read_text())["seed"]
    batch = args.batch or f"policy-{load_policy(policy_path).version}-seed{seed}"

    async def model_loop(name: str) -> list[Path]:
        dirs = []
        for repeat in range(1, args.repeats + 1):       # repeats one after another, models side by side
            judge = LiteLLMJudge(name)
            try:
                d = await run_once(judge=judge, model_name=name, repeat=repeat, batch=batch,
                                   policy_path=policy_path, sample_path=sample_path,
                                   item_concurrency=args.item_concurrency, limit=args.limit)
            finally:
                await judge.aclose()
            print(f"done {d.relative_to(ROOT)}", flush=True)
            dirs.append(d)
        return dirs

    await asyncio.gather(*(model_loop(m) for m in args.models))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--models", nargs="+", choices=sorted(PRESETS), default=sorted(PRESETS))
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--policy", default=str(DEFAULT_POLICY))
    ap.add_argument("--sample", default=str(DEFAULT_SAMPLE))
    ap.add_argument("--batch", default=None, help="folder under runs/ (default: policy-<version>-seed<seed>)")
    ap.add_argument("--item-concurrency", type=int, default=6)
    ap.add_argument("--limit", type=int, default=None, help="only the first N sample items (smoke runs)")
    return asyncio.run(_main(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
