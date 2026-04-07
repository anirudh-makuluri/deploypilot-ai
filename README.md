# SD-Artifacts Repo Analyzer

SD-Artifacts is a FastAPI service that analyzes GitHub repositories and generates deployment artifacts for the detected app services. The analysis pipeline scans repository structure, plans deployable services, generates Dockerfile output, optionally emits `docker-compose.yml`, builds an `nginx.conf`, and runs a verification pass with risk notes and confidence scoring.

The project is built around a LangGraph workflow and supports both one-shot JSON responses and streaming Server-Sent Events for long-running analysis and feedback remediation.

## What It Does

- Scans public or private GitHub repositories.
- Detects deployable services, stack tokens, and likely ports.
- Generates Dockerfiles per service.
- Generates `docker-compose.yml` only when multiple app services need orchestration.
- Generates an `nginx.conf` for reverse proxying and route handling.
- Verifies output with hadolint plus deterministic risk and confidence checks.
- Caches analysis results in Supabase by `repo_url + commit_sha + package_path + service_name`.
- Supports feedback-driven regeneration against an existing cached analysis.
- Supports example-bank retrieval and Dockerfile template management in Supabase.

## Pipeline

```mermaid
graph TD
    Start(("Start")) --> Scan["Scanner"]
    Scan -->|Cache hit| ReturnCache["Return cached payload"]
    Scan -->|Cache miss| Plan["Planner"]
    Plan --> Commands["Command generator"]
    Commands --> Docker["Dockerfile generator"]
    Docker -->|Multi-service| Compose["Compose generator"]
    Docker -->|Single-service| Nginx["Nginx generator"]
    Compose --> Nginx
    Nginx --> Verify["Verifier"]
    Verify --> Save["Save to cache"]
    ReturnCache --> End(("End"))
    Save --> End
```

## Requirements

- Python 3.10+
- A GitHub token for private repositories or higher rate limits
- Amazon Bedrock credentials and model access
- Supabase project credentials if you want cache, example bank, benchmarks, or templates
- `hadolint` installed locally if you want Dockerfile lint output during verification

## Setup

1. Create a virtual environment and install dependencies.

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS/Linux
# source venv/bin/activate
pip install -r requirements.txt
```

2. Install `hadolint`.

- macOS: `brew install hadolint`
- Windows: `scoop install hadolint`
- Other platforms: download from the official GitHub releases page

3. Create a `.env` file in the project root.

```env
AWS_ACCESS_KEY_ID=your_aws_access_key
AWS_SECRET_ACCESS_KEY=your_aws_secret_key
AWS_DEFAULT_REGION=your_aws_region
BEDROCK_MODEL_ID=anthropic.claude-3-haiku-20240307-v1:0

SUPABASE_URL=your_supabase_project_url
SUPABASE_SERVICE_ROLE_KEY=your_supabase_service_role_key

API_BEARER_TOKEN=your_api_token
# Any one of these auth vars works:
# SD_API_BEARER_TOKEN=your_api_token
# API_AUTH_TOKEN=your_api_token

PORT=8080
```

4. Initialize the Supabase schema.

- Run [`supabase_schema.sql`](/C:/Users/aniru/OneDrive/Desktop/own/sd-artifacts/supabase_schema.sql) in the Supabase SQL editor.
- Run [`migrations/create_dockerfile_templates.sql`](/C:/Users/aniru/OneDrive/Desktop/own/sd-artifacts/migrations/create_dockerfile_templates.sql) as well if you want to use the template endpoints.
- `supabase_schema.sql` creates the analysis cache, example bank, and benchmark artifact tables. The template migration creates the `dockerfile_templates` table used by `GET /templates` and the template management endpoints.

5. Start the API.

```bash
python app.py
```

Alternative:

```bash
uvicorn app:app --host 0.0.0.0 --port 8080
```

## Authentication and Cache Behavior

Most mutating or live-compute endpoints require a bearer token header:

```text
Authorization: Bearer <your token>
```

`POST /analyze` and `POST /analyze/stream` have one special case:

- If the request includes `commit_sha` and a matching cached result exists, the response can be served without authentication.
- If the cache lookup misses, the request falls back to live analysis and therefore requires authentication.

This lets clients fetch known cached analyses cheaply while still protecting the expensive GitHub + LLM path.

## Core Request Fields

The main analysis request supports:

- `repo_url`: GitHub repository URL.
- `github_token`: optional token for private repos or higher API limits.
- `max_files`: scan cap, default `50`.
- `package_path`: optional monorepo subpath, default `.`.
- `service_name`: optional selector for a single service inside the analyzed scope.
- `commit_sha`: optional cache key for cache-only retrieval.

`package_path` and `service_name` are especially useful for monorepos, where you may want to analyze one package or one deployable service instead of the whole repository.

## API Overview

Main analysis and remediation:

- `POST /analyze`
- `POST /analyze/stream`
- `POST /feedback`
- `POST /feedback/stream`

Example bank operations:

- `POST /examples/seed`
- `POST /examples/seed/popular`
- `POST /examples/preview`

Cache operations:

- `DELETE /cache`

Template operations:

- `GET /templates`
- `POST /templates`
- `POST /templates/seed`
- `DELETE /templates/{name}`

Detailed examples live in [docs/api-examples.md](/C:/Users/aniru/OneDrive/Desktop/own/sd-artifacts/docs/api-examples.md).

## Response Shape

The primary analysis endpoints return:

- `commit_sha`
- `stack_summary`
- `stack_tokens`
- `services`
- `dockerfiles`
- `docker_compose`
- `nginx_conf`
- `has_existing_dockerfiles`
- `has_existing_compose`
- `risks`
- `confidence`
- `hadolint_results`
- `commands`
- `token_usage`

Streaming endpoints emit `progress`, `complete`, and `error` events as SSE.

## Example Workflows

Analyze a repo:

```bash
curl -X POST http://localhost:8080/analyze \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/user/repo-name",
    "package_path": "."
  }'
```

Stream progress:

```bash
curl -N -X POST http://localhost:8080/analyze/stream \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/user/repo-name"
  }'
```

Iterate on an existing cached analysis:

```bash
curl -X POST http://localhost:8080/feedback \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/user/repo-name",
    "commit_sha": "abc123def456",
    "feedback": "The API service should expose the correct health check and nginx should route /api to the backend."
  }'
```

## Benchmarking

The benchmark runner evaluates:

- scanner and planner quality against labeled repositories
- checked-in artifact quality for Dockerfile, compose, and nginx files
- optionally generated-artifact quality when `--include-generated` is enabled

Run the standard benchmark:

```bash
python tools/evaluate_scan_quality.py \
  --labels-file benchmarks/example_bank_labels.json \
  --max-workers 2
```

Run generated-artifact evaluation:

```bash
python tools/evaluate_scan_quality.py \
  --labels-file benchmarks/example_bank_labels.json \
  --max-workers 2 \
  --include-generated
```

See [docs/quality-and-testing.md](/C:/Users/aniru/OneDrive/Desktop/own/sd-artifacts/docs/quality-and-testing.md) for metrics, thresholds, and output details.

## Testing

Run the test suite:

```bash
python -m pytest tests -q
```

Run the benchmark-focused regression subset:

```bash
python -m pytest tests/test_node_retry_integration.py tests/test_evaluate_scan_quality.py -q
```

## Project Layout

- [`app.py`](/C:/Users/aniru/OneDrive/Desktop/own/sd-artifacts/app.py): FastAPI entrypoint and public endpoints
- [`graph/`](/C:/Users/aniru/OneDrive/Desktop/own/sd-artifacts/graph): LangGraph workflows and node logic
- [`tools/`](/C:/Users/aniru/OneDrive/Desktop/own/sd-artifacts/tools): helpers for examples, evaluation, templates, and metrics
- [`tests/`](/C:/Users/aniru/OneDrive/Desktop/own/sd-artifacts/tests): unit and integration-style tests
- [`docs/`](/C:/Users/aniru/OneDrive/Desktop/own/sd-artifacts/docs): API and operational documentation
- [`benchmarks/`](/C:/Users/aniru/OneDrive/Desktop/own/sd-artifacts/benchmarks): labels, reports, and stack-token references

## Documentation Index

- [API examples](/C:/Users/aniru/OneDrive/Desktop/own/sd-artifacts/docs/api-examples.md)
- [Feedback remediation flow](/C:/Users/aniru/OneDrive/Desktop/own/sd-artifacts/docs/feedback-workflow.md)
- [Retry and timeout strategy](/C:/Users/aniru/OneDrive/Desktop/own/sd-artifacts/docs/retry-timeout-strategy.md)
- [Quality metrics and testing](/C:/Users/aniru/OneDrive/Desktop/own/sd-artifacts/docs/quality-and-testing.md)
- [Stack token reference](/C:/Users/aniru/OneDrive/Desktop/own/sd-artifacts/benchmarks/stack_tokens.md)

## Tech Stack

- FastAPI
- LangGraph and LangChain
- Amazon Bedrock
- GitHub API
- Supabase

## License

MIT
