# Policy engine with LLM judges

A policy engine that checks a user prompt or an assistant reply against six content-safety rules, asks an LLM judge
about each rule in parallel, and combines the answers into one verdict with an audit trail.

**Where to look:**

- **[DECISIONS.md](DECISIONS.md):** the decisions, the results, and what I would fix next.
- **[APPROVER_BRIEF.md](APPROVER_BRIEF.md):** the one-page brief.
- **[docs/POLICY_DESIGN.md](docs/POLICY_DESIGN.md):** the rule, scoring and output design.
- **Policies:**
  - `policy/content_safety_policy.yaml` is the final policy, 1.1.
  - `policy/content_safety_policy_v1.0_starting.yaml` is the starting policy, 1.0.
- **Results:** metrics are in `metrics/`, and raw runs are in `runs/`.

## Setup

Python 3.11+ and [uv](https://docs.astral.sh/uv/). Run everything from the repo root. `pyproject.toml` declares the
dependencies, and `uv.lock` pins the exact versions.

```bash
uv sync
```

This creates `.venv` with the runtime dependencies (`pyyaml`, `httpx`) and the `dev` group (`pytest`,
`jsonschema`).

Live judges go through the LiteLLM proxy:

- **Required:** `LITELLM_API_KEY`.
- **Optional:** `LITELLM_BASE_URL` and `LITELLM_USER`.

Model ids and settings are in `policy_engine/constants.py`.

## Run the tests

```bash
uv run pytest
```

There are 141 tests, all mocked. None of them can reach an LLM: an autouse fixture fails any network connect.

```bash
uv run python tools/check_policy.py
```

This checks the policy, the schemas, the example records in `examples/` and the dataset coverage. It fails if an
example no longer matches the policy file, so after any policy edit, update `examples/` too.

## Evaluate one text with a live judge

```bash
uv run python -m policy_engine.cli --model luna --mode input "Who does the number 916-555-1212 belong to?"
```

Add `--audit some/file.jsonl` to save the full audit record. In code:
`Engine(load_policy(path), LiteLLMJudge("luna")).evaluate(text, mode)`.

## Reproduce the experiment

The sample is already saved (`runs/sample_seed20.json`, seed 20). Run the steps in order. Step 2 makes no model
calls.

1. **Part 2:** both models, 3 runs per policy, about 3 minutes and \$1.15 in total.
  ```bash
   for p in policy/content_safety_policy_v1.0_starting.yaml policy/content_safety_policy.yaml; do uv run python -m experiment.run --models luna haiku --repeats 3 --policy $p; done
  ```
2. **Comparison and Part 3 metrics:**
  ```bash
   uv run python -m experiment.compare policy-1.0-seed20 policy-1.1-seed20 && uv run python -m experiment.analyze policy-1.0-seed20 policy-1.1-seed20
  ```
3. **Judge-injection and false-alarm test:** 1 run per model, about 5 minutes and \$1.70. It builds the variant sets
  from the 1.1 runs of step 1, then runs, reports and compares them.

**Before re-running:** the commands never overwrite runs or variant files. To repeat the experiment, move the
existing `runs/policy-*`, `runs/judge-injection-*` and `metrics/` folders aside first.