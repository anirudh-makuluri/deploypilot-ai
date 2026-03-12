# sd-artifacts — Planned Improvements

## Completed

### [x] 1. Streaming Responses (SSE)
- Replace single POST response with Server-Sent Events
- Stream each node's output as it completes: scanner → planner → dockerfiles → compose → nginx → verifier
- Frontend gets real-time progress instead of waiting for the full pipeline
- Use FastAPI's `StreamingResponse` with `text/event-stream`

### [x] 2. Caching & Rate Limiting
- Cache repo scans by `repo_url + commit_sha` to avoid redundant GitHub API calls
- Use supabase db for storing responses
- Return cached results instantly when available

### [x] 3. Retry Logic & Error Recovery
- Add exponential backoff retries for failed LLM calls
- Retry on malformed output (Pydantic validation failures)
- Set per-node timeout limits
- Fallback to simpler prompts on repeated failures

### [x] 4. Example Bank Grounding (Supabase)
- Added `example_bank` table in Supabase with RLS + indexes for `artifact_type`, `quality_score`, and `stack_tags`.
- Added seeding pipeline (`tools/example_bank.py`) to ingest Dockerfile/compose examples from curated popular repos.
- Added permissive license filtering and upsert behavior keyed by `source_repo + source_path`.
- Added retrieval + ranking logic based on stack tags and quality score.
- Integrated retrieved examples into generation prompts:
  - `graph/nodes/dockerfile_generator.py`
  - `graph/nodes/compose_generator.py`
- Added API endpoints:
  - `POST /examples/seed`
  - `POST /examples/seed/popular`
  - `POST /examples/preview`
- Updated docs (`README.md`) with Supabase setup and example bank usage.

### [x] 5. Benchmark Artifact Evaluation and Regression Coverage
- Added weighted scoring contracts for Dockerfile, compose, and nginx artifacts.
- Extended the benchmark runner to score existing repo artifacts and emit `artifact_summary`.
- Added `--include-generated` mode to score generated artifacts separately via `generated_artifact_summary`.
- Made compose generation conditional on multi-service repos in both the graph and generated benchmark flow.
- Added compose-generation audit metrics: `wrong_compose_gen_rate`, `compose_missing_when_required_count`, and `compose_generated_when_not_required_count`.
- Added regression fixtures and routing tests:
  - `tests/test_artifact_evaluators.py`
  - `tests/test_evaluate_scan_quality.py`
  - `tests/test_graph_flow.py`
- Updated benchmark docs to describe the new scoring and generated-evaluation modes.

## Pending (Sorted by Priority)

### [P0] 1. Feedback Loop for Failed or Low-Quality Output
- Add a feedback endpoint so users can submit free-text feedback when generated deployment artifacts do not work.
- Example feedback payload:
  - `run_id`: ties feedback to a specific generation run
  - `feedback_text`: e.g., "deployment is not working on Cloud Run"
  - `severity`: `blocking | degraded | minor`
  - `artifact_scope`: `dockerfile | compose | nginx | env | unknown`
  - `optional_metadata`: logs, failing command, platform (`ecs`, `cloud_run`, `k8s`), runtime error snippets

- Add a Supabase table `analysis_feedback` (expand the optional table from impact metrics):
  - `id`, `created_at`, `run_id`, `user_id` (optional)
  - `feedback_text`, `severity`, `artifact_scope`
  - `resolved` (bool), `resolution_note`
  - `workflow_version`, `retry_attempt`

- Add a `feedback_interpreter_node` before regeneration:
  - Classify feedback into actionable categories:
    - `build_failure`
    - `runtime_crash`
    - `healthcheck_failure`
    - `port_mismatch`
    - `missing_env_var`
    - `reverse_proxy_misconfig`
  - Extract constraints from text/log snippets and create a `repair_plan` object.

- Add a `targeted_regeneration` flow instead of full rerun by default:
  - If `port_mismatch`, rerun `compose_generator` + `nginx_generator` + `verifier`
  - If `missing_env_var`, rerun `env_extractor` + `compose_generator` + `verifier`
  - If `build_failure`, rerun only `dockerfile_generator` + `verifier`
  - Fall back to full pipeline if confidence in classification is low

- Prompt adaptation:
  - Inject `feedback_summary` and `repair_plan` into generation prompts
  - Add explicit "previous output failed because ..." constraints
  - Require generators to explain what changed to address feedback

- Add "delta output" in API response:
  - `changes_made`: list of concrete fixes applied
  - `feedback_addressed`: bool
  - `remaining_risks`: any unresolved items
  - `suggested_next_debug_step`: if still failing

- Learning loop:
  - Persist `(feedback_category, stack_tags, successful_fix_pattern)` into `example_bank` metadata or a new `repair_patterns` table
  - During future runs, retrieve prior successful repair patterns for similar stacks/issues
  - Track KPI: `% feedback tickets resolved in first retry`

### [P1] 2. Environment Variable Extraction
- New `env_extractor_node` that parses codebase for env var references
- Scan `process.env.*`, `os.getenv()`, `.env` files, and config files
- Generate a `.env.example` with all required variables, defaults, and descriptions
- Flag missing vars in compose and Dockerfiles

### [P2] 3. Impact Metrics for Grounded Generation
- Add run telemetry to measure whether `example_bank` grounding actually improves output quality over prompt-only generation.
- Add a Supabase table `analysis_runs` with fields:
  - `id`, `created_at`, `repo_url`, `commit_sha`, `package_path`
  - `strategy` (`prompt_only` | `grounded_examples`)
  - `detected_stack`, `services_count`
  - `token_input`, `token_output`, `token_total`
  - `latency_ms`, `example_count_used`, `example_retrieval_ms`
- Add a Supabase table `analysis_quality_metrics` keyed by `run_id` with fields:
  - `hadolint_warning_count`, `hadolint_error_count`
  - `risk_count`, `confidence`
  - `compose_valid`, `port_consistency_pass`
  - `non_root_services_count`, `healthcheck_services_count`, `multi_stage_services_count`
- Instrumentation points:
  - `app.py`: start/end timing, strategy label, token totals, persist `analysis_runs`
  - `graph/nodes/dockerfile_generator.py`: count retrieved examples per service
  - `graph/nodes/compose_generator.py`: count compose examples used
  - `graph/nodes/verifier.py`: convert hadolint output to numeric counters + compose validity checks
- Optional human feedback table `analysis_feedback`:
  - `run_id`, `accepted`, `manual_edits_required`, `manual_fix_minutes`, `notes`
- Report weekly KPIs:
  - Median hadolint warnings per run
  - Median verifier risk count
  - `% runs with confidence >= 0.85`
  - `% runs with compose valid + port consistency pass`
  - Median manual fix minutes

### [P3] 4. Deployment README Generation
- New `readme_generator_node` at the end of the pipeline
- Generate a `DEPLOY.md` with step-by-step deployment instructions
- Include: prerequisites, env setup, build commands, and common troubleshooting
- Tailored to the detected stack and generated configs

### [P4] 5. Cost Estimation
- Calculate `$` cost from token usage using model pricing
- Include `cost_usd` field in API response
- Support pricing for different models (Haiku, Sonnet, etc.)
- Track per-node cost breakdown for optimization
