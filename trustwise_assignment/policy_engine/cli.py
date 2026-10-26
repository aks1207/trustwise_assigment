"""Evaluate one text from the command line with a live judge.

    python -m policy_engine.cli --model luna --mode input "Who does 916-555-1212 belong to?"

Prints the safe result. With --audit PATH, appends the full audit record to that JSONL file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .audit import AuditLog
from .engine import Engine
from .litellm_judge import PRESETS, LiteLLMJudge
from .policy import load_policy

DEFAULT_POLICY = Path(__file__).resolve().parents[1] / "policy" / "content_safety_policy.yaml"


async def _main(args: argparse.Namespace) -> int:
    judge = LiteLLMJudge(args.model)
    sink = AuditLog(args.audit) if args.audit else None
    try:
        out = await Engine(load_policy(args.policy), judge, audit_sink=sink).evaluate(args.text, args.mode)
    finally:
        await judge.aclose()
    print(json.dumps(out.result, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("text")
    ap.add_argument("--model", choices=sorted(PRESETS), default="luna")
    ap.add_argument("--mode", choices=["input", "output"], default="input")
    ap.add_argument("--policy", default=str(DEFAULT_POLICY))
    ap.add_argument("--audit", help="append the audit record to this JSONL file")
    return asyncio.run(_main(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
