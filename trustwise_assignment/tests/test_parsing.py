"""Judge reply parsing: what is tolerated and what is a parse failure."""

import pytest

from policy_engine.parsing import parse_judge_response

GOOD = '{"verdict": "FAIL", "confidence": 0.8, "reasoning": "A phone number."}'


@pytest.mark.parametrize("raw", [
    GOOD,
    f"```json\n{GOOD}\n```",
    f"Here is my answer:\n{GOOD}\nThanks.",
    '{"verdict": "fail", "confidence": 0.8, "reasoning": "A phone number."}',
    '{"verdict": " FAIL ", "confidence": 0.8, "reasoning": "A phone number."}',
])
def test_tolerated(raw):
    r = parse_judge_response(raw)
    assert r.status == "ok"
    assert r.parsed == {"verdict": "FAIL", "confidence": 0.8, "reasoning": "A phone number."}


@pytest.mark.parametrize("raw,status", [
    ("", "invalid_json"),
    ("PASS", "invalid_json"),
    ('{"verdict": "PASS", "confidence": 0.9, "reasoning": "x"', "invalid_json"),        # truncated
    ('[{"verdict": "PASS", "confidence": 0.9, "reasoning": "x"}]', "invalid_json"),      # not an object
    (f"{GOOD} {GOOD}", "invalid_json"),                                                  # two objects
    ('{"verdict": "PASS", "reasoning": "x"}', "missing_fields"),
    ('{"confidence": 0.9}', "missing_fields"),
    ('{"verdict": "PASS", "confidence": 0.9, "reasoning": "x", "score": 1}', "extra_fields"),
    ('{"verdict": "SAFE", "confidence": 0.9, "reasoning": "x"}', "invalid_values"),
    ('{"verdict": "PASS", "confidence": 1.7, "reasoning": "x"}', "invalid_values"),      # no clamping
    ('{"verdict": "PASS", "confidence": -0.1, "reasoning": "x"}', "invalid_values"),
    ('{"verdict": "PASS", "confidence": "0.9", "reasoning": "x"}', "invalid_values"),
    ('{"verdict": "PASS", "confidence": true, "reasoning": "x"}', "invalid_values"),
    ('{"verdict": "PASS", "confidence": NaN, "reasoning": "x"}', "invalid_values"),
    ('{"verdict": "PASS", "confidence": 0.9, "reasoning": null}', "invalid_values"),
])
def test_rejected(raw, status):
    r = parse_judge_response(raw)
    assert r.status == status
    assert r.parsed is None
    assert r.message


def test_long_reasoning_is_accepted():
    # No length limit for now; to be revisited with Part 2 data.
    r = parse_judge_response('{"verdict": "PASS", "confidence": 0.9, "reasoning": "' + "x" * 5000 + '"}')
    assert r.status == "ok" and len(r.parsed["reasoning"]) == 5000


def test_integer_confidence_is_accepted_as_float():
    r = parse_judge_response('{"verdict": "PASS", "confidence": 1, "reasoning": "x"}')
    assert r.status == "ok" and r.parsed["confidence"] == 1.0
