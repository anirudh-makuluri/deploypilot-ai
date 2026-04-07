# Feedback Remediation Workflow

The feedback pipeline lets you revise generated artifacts without re-running a full repository scan. It operates on an existing cached analysis identified by `repo_url + commit_sha + package_path`.

## When to Use It

Use `POST /feedback` or `POST /feedback/stream` when:

- generated Dockerfiles, compose output, or nginx config need targeted fixes
- you want to preserve the same commit context and iterate on the artifacts
- you already have a cached analysis for the target repository revision

## Execution Flow

1. The API loads the cached analysis row from Supabase.
2. The feedback coordinator reads the user feedback, prior risks, and prior hadolint results.
3. The coordinator emits a per-artifact improvement plan with `should_change` decisions and instructions.
4. Artifact improver nodes update Dockerfile, compose, and nginx output only where needed.
5. The feedback verifier re-runs linting, risk filtering, and confidence scoring.
6. The improved result is returned and upserted back into the cache.

## Inputs

- `repo_url`
- `commit_sha`
- `package_path` (optional, defaults to `.`)
- `feedback`
- `github_token` (accepted by the request model, though the workflow primarily operates on cached content)

## Outputs

The response matches the main analysis shape and includes:

- `dockerfiles`
- `docker_compose`
- `nginx_conf`
- `risks`
- `confidence`
- `hadolint_results`
- `commands`
- `stack_summary`
- `stack_tokens`
- `services`

## Streaming Behavior

`POST /feedback/stream` emits SSE events with:

- `progress` for each feedback node
- `complete` with the final payload
- `error` if cache lookup or remediation fails

Typical node names are:

- `feedback_coordinator`
- `dockerfile_improver`
- `compose_improver`
- `nginx_improver`
- `feedback_verifier`

## Failure Handling

- All LLM-backed nodes use the shared retry wrapper with exponential backoff and jitter.
- Structured-output and validation failures are retried.
- Timeout budgets limit per-node runtime.
- If coordinator planning fails, the workflow falls back to a permissive plan so remediation can still proceed.
- If the cache row does not exist, the feedback endpoints fail instead of running a fresh analysis.

## Practical Guidance

- Keep feedback concrete and deployment-specific.
- Mention service names, ports, routes, startup commands, and health checks when possible.
- Use streaming mode if you want progress visibility in a dashboard or CLI.
- Treat feedback as iterative artifact repair, not as a substitute for a missing initial `/analyze` run.
