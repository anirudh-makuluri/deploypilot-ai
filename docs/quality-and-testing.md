# Quality and Testing

This document covers test execution and objective scan-quality benchmarking.

## Automated Tests

Current tests include:
- `tests/test_app_endpoints.py`: API endpoint behavior and response contracts.
- `tests/test_artifact_evaluators.py`: regression fixtures for Dockerfile, compose, and nginx artifact scoring.
- `tests/test_feedback_workflow.py`: feedback coordinator and remediation behavior.
- `tests/test_llm_retry.py`: retry wrapper behavior and exhaustion paths.
- `tests/test_node_retry_integration.py`: retry integration across graph execution.
- `tests/test_port_and_stack_extractor.py`: stack token and port extraction logic.
- `tests/test_eval_metrics.py`: scan quality metric calculations.
- `tests/test_evaluate_scan_quality.py`: end-to-end benchmark script behavior.
- `tests/test_graph_flow.py`: graph routing behavior, including conditional compose generation.
- `tests/test_github_tools.py`: GitHub utility behavior during scanning.

Run tests:
```bash
pip install pytest
python -m pytest tests -q
```

Run a specific module:
```bash
python -m pytest tests/test_app_endpoints.py -q
```

## Scan Quality Benchmarking

The benchmark runner evaluates two layers of quality against a labels file:
- Scanner and planner quality: service detection, mobile leakage, stack labeling, and known-port accuracy.
- Artifact quality: Dockerfile, compose, and nginx scoring for repo files already checked into the target repository.

When `--include-generated` is enabled, the runner also executes the generator nodes and scores the generated artifacts separately.

### 1) Prepare labels

Create `benchmarks/example_bank_labels.json` using `benchmarks/example_bank_labels.sample.json` as a template.

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
  --max-files 50
```

Include generated artifact evaluation:

```bash
python tools/evaluate_scan_quality.py \
  --labels-file benchmarks/example_bank_labels.json \
  --max-files 50 \
  --include-generated
```

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

### 5) Latest committed planner snapshot

From `benchmarks/latest-scan-quality.json`:
- Targets evaluated: 18
- `service_precision`: 0.9583
- `service_recall`: 0.9583
- `service_f1`: 0.9583
- `mobile_leakage_rate`: 0.0
- `stack_accuracy`: 1.0
- `port_accuracy_known`: 0.9167 (22/24)
- `port_unknown_rate`: 0.0417
- Failure buckets: 17 `ok`, 1 `service_precision_miss`

Note:
- The committed snapshot above predates generated-artifact benchmarking.
- To inspect `artifact_summary`, `generated_artifact_summary`, and compose-generation audit metrics, run the benchmark locally with the current script.

Current remaining precision miss:
- `anirudh-makuluri/Accio` at `package_path=.` predicts one extra service (`neo4j` at `.`) in addition to expected backend and frontend services.

## Stack Tokens

Stack token definitions are centralized to keep scanning, port inference, and benchmark labeling consistent.

- Code registry: `tools/stack_tokens.py`
- Human reference: `benchmarks/stack_tokens.md`

Use canonical tokens from this registry in `required_stack_tokens` label entries.
