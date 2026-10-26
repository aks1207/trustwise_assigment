"""Load and validate a policy file.

The loader is strict: a policy that would make the engine guess (unknown strategy, weight out of range,
missing template placeholder) is rejected at load time, not at the first evaluation.
"""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

STRATEGIES = ("all", "any", "weighted_threshold")
ON_FAIL_ACTIONS = ("block", "redact")
MODES = ("input", "output")
VERDICT_ORDER = ("ALLOW", "REDACT", "REVIEW", "BLOCK")
CONTENT_ESCAPING = ("none", "html")   # html: & < > become &amp; &lt; &gt;, so content cannot contain a tag
USER_TEMPLATE_PLACEHOLDERS = ("{rule_id}", "{description}", "{mode}", "{mode_note}", "{nonce}", "{content}")


class PolicyError(ValueError):
    """The policy file is missing something or has a value the engine cannot act on."""


@dataclass(frozen=True)
class Rule:
    id: str
    description: str
    judge_prompt: str
    on_fail: dict[str, str]          # mode -> block | redact, one entry per mode in applies_to
    weight: float
    applies_to: tuple[str, ...]
    category: str | None = None

    def action_for(self, mode: str) -> str:
        """The on_fail action for this mode. The policy may give one action or one per mode."""
        return self.on_fail[mode]


@dataclass(frozen=True)
class JudgeSettings:
    temperature: float = 0.0
    max_output_tokens: int = 4096
    timeout_s: float = 20.0
    max_retries: int = 2
    retry_backoff_s: tuple[float, ...] = (0.5, 2.0)

    def backoff(self, retry_number: int) -> float:
        """Delay before retry `retry_number` (1-based). The last value repeats."""
        if not self.retry_backoff_s:
            return 0.0
        return self.retry_backoff_s[min(retry_number, len(self.retry_backoff_s)) - 1]


@dataclass(frozen=True)
class Policy:
    name: str
    version: str
    default_action: str
    evaluation_strategy: str
    threshold: float
    review_margin: float
    max_content_chars: int
    content_escaping: str          # "none" | "html": how content is escaped before it goes into the judge prompt
    judge: JudgeSettings
    evaluation_deadline_s: float | None
    judge_system_prompt: str
    judge_user_template: str
    mode_notes: dict[str, str]
    rules: tuple[Rule, ...]
    sha256: str
    raw: dict[str, Any] = field(repr=False, compare=False)

    def rules_for(self, mode: str) -> list[Rule]:
        return [r for r in self.rules if mode in r.applies_to]

    def with_overrides(self, **changes: Any) -> "Policy":
        """Copy with e.g. threshold or evaluation_strategy changed, for sweeps. sha256 still names the file."""
        bad = set(changes) - {"threshold", "evaluation_strategy", "review_margin"}
        if bad:
            raise PolicyError(f"cannot override {sorted(bad)}")
        updated = dataclasses.replace(self, **changes)
        _check_strategy(updated.evaluation_strategy, updated.threshold, updated.review_margin)
        return updated


def load_policy(path: str | Path) -> Policy:
    data = Path(path).read_bytes()
    return parse_policy(data)


def parse_policy(data: bytes) -> Policy:
    try:
        raw = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyError("policy must be a mapping")

    for key in ("name", "version", "default_action", "evaluation_strategy", "threshold",
                "judge_system_prompt", "judge_user_template", "mode_notes", "rules"):
        if key not in raw:
            raise PolicyError(f"policy missing required field '{key}'")

    if raw["default_action"] != "allow":
        raise PolicyError("default_action must be 'allow' (the engine has no other default)")
    if tuple(raw.get("verdict_order", VERDICT_ORDER)) != VERDICT_ORDER:
        raise PolicyError(f"verdict_order must be {list(VERDICT_ORDER)}")

    scoring = raw.get("scoring") or {}
    if scoring.get("method", "noisy_or") != "noisy_or":
        raise PolicyError("only scoring.method 'noisy_or' is implemented")
    review_margin = float(scoring.get("review_margin", 0.0))
    _check_strategy(raw["evaluation_strategy"], raw["threshold"], review_margin)

    engine = raw.get("engine") or {}
    if engine.get("on_judge_error", "fail_closed") != "fail_closed":
        raise PolicyError("only on_judge_error 'fail_closed' is implemented")
    if engine.get("uncertain_handling", "review") != "review":
        raise PolicyError("only uncertain_handling 'review' is implemented")
    j = engine.get("judge") or {}
    judge = JudgeSettings(
        temperature=float(j.get("temperature", 0.0)),
        max_output_tokens=int(j.get("max_output_tokens", 4096)),
        timeout_s=float(j.get("timeout_s", 20.0)),
        max_retries=int(j.get("max_retries", 2)),
        retry_backoff_s=tuple(float(x) for x in j.get("retry_backoff_s", (0.5, 2.0))),
    )
    if judge.timeout_s <= 0 or judge.max_retries < 0 or judge.max_output_tokens <= 0:
        raise PolicyError("engine.judge timeout_s > 0, max_retries >= 0, max_output_tokens > 0 required")
    deadline = engine.get("evaluation_deadline_s")
    escaping = engine.get("content_escaping", "none")
    if escaping not in CONTENT_ESCAPING:
        raise PolicyError(f"engine.content_escaping must be one of {list(CONTENT_ESCAPING)}")

    template = raw["judge_user_template"]
    missing = [p for p in USER_TEMPLATE_PLACEHOLDERS if p not in template]
    if missing:
        raise PolicyError(f"judge_user_template missing placeholders {missing}")
    if set(raw["mode_notes"]) != set(MODES):
        raise PolicyError("mode_notes must have exactly 'input' and 'output'")

    rules = tuple(_parse_rule(r) for r in raw["rules"])
    if not rules:
        raise PolicyError("policy has no rules")
    ids = [r.id for r in rules]
    if len(ids) != len(set(ids)):
        raise PolicyError("duplicate rule ids")
    for mode in MODES:
        if not any(mode in r.applies_to for r in rules):
            raise PolicyError(f"no rule applies to mode '{mode}'")

    return Policy(
        name=str(raw["name"]),
        version=str(raw["version"]),
        default_action="allow",
        evaluation_strategy=raw["evaluation_strategy"],
        threshold=float(raw["threshold"]),
        review_margin=review_margin,
        max_content_chars=int(engine.get("max_content_chars", 8000)),
        content_escaping=escaping,
        judge=judge,
        evaluation_deadline_s=None if deadline is None else float(deadline),
        judge_system_prompt=raw["judge_system_prompt"],
        judge_user_template=template,
        mode_notes=dict(raw["mode_notes"]),
        rules=rules,
        sha256=hashlib.sha256(data).hexdigest(),
        raw=raw,
    )


def _check_strategy(strategy: str, threshold: Any, review_margin: float) -> None:
    if strategy not in STRATEGIES:
        raise PolicyError(f"evaluation_strategy must be one of {STRATEGIES}, got {strategy!r}")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0 < threshold <= 1:
        raise PolicyError(f"threshold must be a number in (0, 1], got {threshold!r}")
    if not 0 <= review_margin < 1:
        raise PolicyError("scoring.review_margin must be in [0, 1)")


def _parse_rule(r: Any) -> Rule:
    if not isinstance(r, dict):
        raise PolicyError("each rule must be a mapping")
    for key in ("id", "description", "judge_prompt", "on_fail", "weight", "applies_to"):
        if key not in r:
            raise PolicyError(f"rule {r.get('id', '?')} missing required field '{key}'")
    rid = r["id"]
    w = r["weight"]
    if not isinstance(w, (int, float)) or isinstance(w, bool) or not 0 < w <= 1:
        raise PolicyError(f"rule {rid}: weight must be in (0, 1], got {w!r}")
    applies = tuple(r["applies_to"])
    if not applies or not set(applies) <= set(MODES):
        raise PolicyError(f"rule {rid}: applies_to must be a non-empty subset of {MODES}")
    if not str(r["judge_prompt"]).strip():
        raise PolicyError(f"rule {rid}: judge_prompt is empty")
    on_fail = _parse_on_fail(rid, r["on_fail"], applies)
    return Rule(
        id=str(rid),
        description=str(r["description"]),
        judge_prompt=str(r["judge_prompt"]),
        on_fail=on_fail,
        weight=float(w),
        applies_to=applies,
        category=r.get("category"),
    )


def _parse_on_fail(rid: str, value: Any, applies: tuple[str, ...]) -> dict[str, str]:
    """`on_fail: block` applies to every mode; `on_fail: {input: redact, output: block}` sets one per mode.

    A per-mode mapping must name exactly the modes in applies_to, so no mode is left without an action and no
    action is set for a mode the rule never runs on.
    """
    if isinstance(value, str):
        mapping = {m: value for m in applies}
    elif isinstance(value, dict):
        if set(value) != set(applies):
            raise PolicyError(f"rule {rid}: per-mode on_fail must name exactly {list(applies)}, got {sorted(value)}")
        mapping = {m: value[m] for m in applies}
    else:
        raise PolicyError(f"rule {rid}: on_fail must be an action or a mapping of mode to action")
    bad = sorted({a for a in mapping.values() if a not in ON_FAIL_ACTIONS})
    if bad:
        raise PolicyError(f"rule {rid}: on_fail must be one of {ON_FAIL_ACTIONS}, got {bad}")
    return mapping
