import asyncio
import itertools
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from policy_engine import Engine, ScriptedJudge, load_policy

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "policy" / "content_safety_policy.yaml"
SCHEMAS = ROOT / "schemas"


# ------------------------------------------------------------------ no live LLM, ever
@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any attempt to open a network connection fails the test."""
    def refuse(*args, **kwargs):
        raise AssertionError("tests must not open network connections")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# ------------------------------------------------------------------ policy + schemas
@pytest.fixture(scope="session")
def policy():
    return load_policy(POLICY_PATH)


@pytest.fixture(scope="session")
def validators():
    resources, schemas = [], {}
    for p in SCHEMAS.glob("*.json"):
        s = json.loads(p.read_text())
        resources.append((s["$id"], Resource.from_contents(s)))
        schemas[p.name] = s
    registry = Registry().with_resources(resources)
    return {
        "result": Draft202012Validator(schemas["evaluation_result.schema.json"], registry=registry),
        "audit": Draft202012Validator(schemas["audit_record.schema.json"], registry=registry),
    }


class SleepRecorder:
    """Stands in for asyncio.sleep in retry backoff so tests do not wait."""
    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


def deterministic_engine(policy, judge, **kwargs):
    ids = (f"00000000-0000-4000-8000-{i:012d}" for i in itertools.count(1))
    nonces = (f"nonce{i:04d}" for i in itertools.count(1))
    fixed = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    kwargs.setdefault("sleep", SleepRecorder())
    return Engine(
        policy, judge,
        id_factory=lambda: next(ids),
        nonce_factory=lambda: next(nonces),
        clock=lambda: fixed,
        **kwargs,
    )


@pytest.fixture
def make_engine(policy):
    def factory(judge=None, policy_override=None, **kwargs):
        return deterministic_engine(policy_override or policy, judge or ScriptedJudge(), **kwargs)
    return factory


@pytest.fixture
def evaluate(validators):
    """Run an evaluation and check both records against the JSON Schemas every time."""
    def run(engine, text, mode="input"):
        out = asyncio.run(engine.evaluate(text, mode))
        for name, record in (("result", out.result), ("audit", out.audit)):
            errors = [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in validators[name].iter_errors(record)]
            assert not errors, f"{name} violates schema: {errors[:3]}"
        assert out.audit["result"] is out.result
        return out
    return run
