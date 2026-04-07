# Quality and Testing

This document covers test execution and objective scan-quality benchmarking.

## Automated Tests

Current tests include:
- `tests/test_app_endpoints.py`: API endpoint behavior and response contracts.
- `tests/test_artifact_evaluators.py`: shared artifact evaluator regression fixtures.
- `tests/test_commands_node.py`: generated install/build/run command selection.
- `tests/test_feedback_workflow.py`: feedback coordinator and remediation behavior.
- `tests/test_llm_retry.py`: retry wrapper behavior and exhaustion paths.
- `tests/test_node_retry_integration.py`: planner and retry integration, including service dedupe and per-service port refinement.
- `tests/test_port_and_stack_extractor.py`: stack token and port extraction logic.
- `tests/test_eval_metrics_nginx.py`: Nginx artifact scoring criteria.
- `tests/test_evaluate_scan_quality.py`: end-to-end benchmark script behavior.
- `tests/test_graph_flow.py`: graph routing behavior, including conditional compose generation.
- `tests/test_github_tools.py`: GitHub utility behavior during scanning.
- `tests/test_planner_service_selection.py`: service selection using `service_name` and scoped paths.
- `tests/test_scanner_cache_scope.py`: package-scoped cache reuse rules.
- `tests/test_verifier_risk_filtering.py`: deterministic verifier risk filtering behavior.

Run tests:
```bash
pip install pytest
python -m pytest tests -q
```

Run a specific module:
```bash
python -m pytest tests/test_app_endpoints.py -q
```

Run the benchmark/planner regression subset:
```bash
python -m pytest tests/test_node_retry_integration.py tests/test_evaluate_scan_quality.py -q
```

Run API and cache-scope coverage:

```bash
python -m pytest tests/test_app_endpoints.py tests/test_scanner_cache_scope.py -q
```

## Scan Quality Benchmarking

The benchmark runner evaluates two layers of quality against a labels file:
- Scanner and planner quality: service detection, mobile leakage, stack labeling, and known-port accuracy.
- Artifact quality: Dockerfile, compose, and nginx scoring for repo files already checked into the target repository.

The benchmark tooling is especially useful after changing:

- stack token extraction
- planner service-selection rules
- port inference
- Dockerfile, compose, or nginx generation behavior
- verifier thresholds or artifact scorers

When `--include-generated` is enabled, the runner also executes the generator nodes and scores the generated artifacts separately.

### 1) Prepare labels

Create `benchmarks/example_bank_labels.json` using `benchmarks/example_bank_labels.sample.json` as a template.
If you use a different filename, pass it via `--labels-file path/to/labels.json`.

Label fields:
- `repo_url` (preferred full GitHub URL)
- `repo` (optional `owner/repo` when `repo_url` is omitted)
- `package_path` (optional subpath, default `.`)
- `required_stack_tokens` (canonical expected stack tokens)
- `expected_services` (ground-truth deployable services)
- `excluded_services` (services that must be excluded)
- `expected_ports` (optional known ports by service)
- `artifact_expectations` (optional future-facing artifact-specific expectations)
- `artifact_scoring_overrides` (optional future-facing per-artifact scoring overrides)

Notes:
- Targets are evaluated as `repo_url + package_path`.
- Monorepo subpaths can be labeled independently.

### 2) Run benchmark

Planner and repo-artifact evaluation:

```bash
python tools/evaluate_scan_quality.py \
  --labels-file benchmarks/example_bank_labels.json \
  --max-files 50 \
  --max-workers 2
```

Include generated artifact evaluation:

```bash
python tools/evaluate_scan_quality.py \
  --labels-file benchmarks/example_bank_labels.json \
  --max-files 50 \
  --max-workers 2 \
  --include-generated
```

Concurrency control:
- `--max-workers` controls how many labeled repos are evaluated concurrently.
- Default is `1` (sequential behavior).
- Use `2` to `4` for typical I/O-bound benchmark runs.
- Results are still reported in input target order.

Behavior:
- Only label-file entries are evaluated.
- `--repos` acts as a filter over labeled entries.
- Output is written to `benchmarks/scan-quality-<timestamp>.json`.
- `benchmarks/latest-scan-quality.json` is refreshed on each run.
- Existing artifact scoring selects the package-local Dockerfile/compose/nginx file when multiple candidates exist.
- In generated mode, Dockerfiles are always evaluated, compose is only expected when `len(expected_services) > 1`, and nginx is always evaluated.

### 3) Metrics reported

- `service_precision`
- `service_recall`
- `service_f1`
- `mobile_leakage_rate`
- `stack_accuracy`
- `port_accuracy_known`
- `port_unknown_rate`
- `artifact_summary` with per-artifact `scored_repo_count`, `avg_total_score`, `pass_rate`, and `pass_threshold`
- `artifact_summary.combined` with average score across all present artifacts and `all_present_artifacts_pass_rate`
- `generated_artifact_summary` when `--include-generated` is enabled
- `wrong_compose_gen_rate` when `--include-generated` is enabled
- `compose_missing_when_required_count` when `--include-generated` is enabled
- `compose_generated_when_not_required_count` when `--include-generated` is enabled
- Repo-level TP/FP/FN mismatch details

Artifact thresholds currently enforced by the scorer contract:
- Dockerfile: `0.90`
- Compose: `0.90`
- Nginx: `0.85`

Generated-mode compose audit logic:
- Compose is required only when the labeled repo has more than one expected deployable service.
- `wrong_compose_gen_rate` counts both missing compose files for multi-service repos and unnecessary compose files for single-service repos.

### 4) Recommended quality gates

- `service_precision >= 0.92`
- `service_recall >= 0.90`
- `mobile_leakage_rate <= 0.02`
- `stack_accuracy >= 0.90` (on labeled repos)
- `port_accuracy_known >= 0.90`
- Generated Dockerfile `avg_total_score >= 0.90`
- Generated Compose `avg_total_score >= 0.90`
- Generated Nginx `avg_total_score >= 0.85`
- `wrong_compose_gen_rate == 0.0`

### 5) Latest benchmark snapshot

From `benchmarks/latest-scan-quality.json` (run `20260407-020210`, generated `2026-04-07T02:02:10.661596+00:00`, 18 labeled targets):

**Planner metrics:**
- `service_precision`: 0.9545
- `service_recall`: 0.8750
- `service_f1`: 0.9130
- `mobile_leakage_rate`: 0.0
- `stack_accuracy`: 1.0
- `port_accuracy_known`: 0.8333 (20/24)
- `port_unknown_rate`: 0.0417
- `wrong_compose_gen_rate`: 0.0
- Failure buckets: `ok = 16`, `port_mismatch = 1`, `service_recall_miss = 1`

**Checked-in artifact metrics:**
- Dockerfile: avg = 0.6179, pass_rate = 0.2857 (threshold 0.90)
- Compose: avg = 0.8167, pass_rate = 0.6667 (threshold 0.90)
- Nginx: avg = 0.1611, pass_rate = 0.0 (threshold 0.85)
- Combined: avg = 0.5953, all_present_pass_rate = 0.2

**Scope note:** This snapshot reflects the repo-artifact evaluation present in `benchmarks/latest-scan-quality.json`. Generated-artifact summary fields are only populated when the benchmark is run with `--include-generated`.

## Operational Notes

- Many tests are pure unit tests and do not require network access.
- Endpoint tests use fakes and monkeypatching rather than real Supabase or Bedrock calls.
- Full analysis and benchmark runs may require valid credentials, GitHub access, and `hadolint` if you want lint-backed verification details.
- Cached analysis behavior is part of the public contract, so cache-key changes should be treated as API-affecting changes.

## Stack Tokens

Stack token definitions are centralized to keep scanning, port inference, and benchmark labeling consistent.

- Code registry: `tools/stack_tokens.py`
- Human reference: `benchmarks/stack_tokens.md`

Use canonical tokens from this registry in `required_stack_tokens` label entries.
