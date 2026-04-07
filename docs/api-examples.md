# API Examples

This document covers the current public API exposed by [`app.py`](/C:/Users/aniru/OneDrive/Desktop/own/sd-artifacts/app.py).

## Base URL

- Local: `http://localhost:8080`

## Authentication

Authenticated endpoints expect:

```text
Authorization: Bearer <token>
```

The token must match one of:

- `SD_API_BEARER_TOKEN`
- `API_BEARER_TOKEN`
- `API_AUTH_TOKEN`

Auth rules by endpoint:

- `POST /analyze` and `POST /analyze/stream` require auth for live analysis.
- `POST /analyze` and `POST /analyze/stream` can be used without auth only when `commit_sha` is supplied and a matching cached payload exists.
- `POST /feedback`, `POST /feedback/stream`, `POST /examples/seed`, `POST /examples/seed/popular`, `DELETE /cache`, `POST /templates`, `POST /templates/seed`, and `DELETE /templates/{name}` require auth.
- `POST /examples/preview` and `GET /templates` do not require auth.

## Analyze Repository

Endpoint: `POST /analyze`

Purpose:

- Runs scanner, planner, generators, and verifier.
- Returns a cached result immediately when `commit_sha` resolves to an existing cache row.

Example:

```bash
curl -X POST http://localhost:8080/analyze \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/user/repo-name",
    "github_token": "ghp_optional",
    "max_files": 50,
    "package_path": ".",
    "service_name": null
  }'
```

Scoped monorepo example:

```bash
curl -X POST http://localhost:8080/analyze \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/user/monorepo",
    "package_path": "apps/web"
  }'
```

Cache-only lookup example:

```bash
curl -X POST http://localhost:8080/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/user/repo-name",
    "commit_sha": "abc123def456",
    "package_path": "."
  }'
```

Request fields:

- `repo_url`: required GitHub repository URL.
- `github_token`: optional GitHub token.
- `max_files`: optional scan cap, default `50`.
- `package_path`: optional repo subpath, default `.`.
- `service_name`: optional service selector within the chosen scope.
- `commit_sha`: optional cache key for cache-first retrieval.

Representative response:

```json
{
  "commit_sha": "abc123def456",
  "stack_summary": "Next.js frontend with FastAPI backend",
  "stack_tokens": ["next", "react", "python", "fastapi"],
  "services": [
    {
      "name": "web",
      "build_context": "apps/web",
      "port": 3000
    }
  ],
  "dockerfiles": {
    "apps/web/Dockerfile": "FROM node:20-alpine\n..."
  },
  "docker_compose": null,
  "nginx_conf": "events {}\nhttp { ... }",
  "has_existing_dockerfiles": false,
  "has_existing_compose": false,
  "risks": [],
  "confidence": 0.93,
  "hadolint_results": {
    "apps/web/Dockerfile": ""
  },
  "commands": {
    "install": "npm install",
    "build": "npm run build",
    "run": "npm start"
  },
  "token_usage": {
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0
  }
}
```

Notes:

- Cache rows are keyed by `repo_url + commit_sha + package_path + service_name`.
- Internal cache metadata such as `_cache_package_path` is stripped from API responses.
- If a cache lookup misses and the request is unauthenticated, the endpoint returns `401`.

## Analyze Repository (Streaming)

Endpoint: `POST /analyze/stream`

Purpose:

- Runs the same analysis pipeline but emits Server-Sent Events.
- Cache hits still emit synthetic progress events before `complete`.

Example:

```bash
curl -N -X POST http://localhost:8080/analyze/stream \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/user/repo-name",
    "package_path": "."
  }'
```

SSE event shape:

```text
event: progress
data: {"node":"scanner","status":"completed"}

event: progress
data: {"node":"planner","status":"completed"}

event: complete
data: { ... full JSON response ... }
```

Possible event types:

- `progress`
- `complete`
- `error`

## Feedback Remediation

Endpoint: `POST /feedback`

Purpose:

- Regenerates artifacts against an existing cached analysis.
- Reuses the original cached analysis for the same `repo_url + commit_sha + package_path`.
- Upserts the improved result back into cache.

Example:

```bash
curl -X POST http://localhost:8080/feedback \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/user/repo-name",
    "commit_sha": "abc123def456",
    "package_path": ".",
    "feedback": "The API service should expose port 8000 and nginx should forward /api to the backend."
  }'
```

Notes:

- A matching cached analysis must already exist.
- Response shape matches `POST /analyze`.
- The upsert currently writes back with `service_name = null`.

## Feedback Remediation (Streaming)

Endpoint: `POST /feedback/stream`

Purpose:

- Returns feedback remediation progress through SSE.

Example:

```bash
curl -N -X POST http://localhost:8080/feedback/stream \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/user/repo-name",
    "commit_sha": "abc123def456",
    "feedback": "Keep the same Dockerfile but fix nginx routing and health checks."
  }'
```

Typical node names:

- `feedback_coordinator`
- `dockerfile_improver`
- `compose_improver`
- `nginx_improver`
- `feedback_verifier`

## Seed Example Bank

Endpoint: `POST /examples/seed`

Purpose:

- Seeds Supabase example-bank rows from an explicit list of repositories.

Example:

```bash
curl -X POST http://localhost:8080/examples/seed \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_urls": [
      "https://github.com/vercel/next.js",
      "https://github.com/tiangolo/full-stack-fastapi-template"
    ],
    "github_token": "ghp_optional",
    "max_files_per_repo": 20,
    "permissive_only": true
  }'
```

## Seed Popular Example Bank

Endpoint: `POST /examples/seed/popular`

Purpose:

- Seeds from the built-in `POPULAR_EXAMPLE_REPOS` list.

Example:

```bash
curl -X POST http://localhost:8080/examples/seed/popular \
  -H "Authorization: Bearer $API_BEARER_TOKEN"
```

## Preview Retrieved Examples

Endpoint: `POST /examples/preview`

Purpose:

- Shows the examples that would be injected into Dockerfile or compose prompts.

Example:

```bash
curl -X POST http://localhost:8080/examples/preview \
  -H "Content-Type: application/json" \
  -d '{
    "artifact_type": "dockerfile",
    "detected_stack": "Next.js app with Node backend",
    "stack_tokens": ["node", "next", "react"],
    "service": {"name": "web", "build_context": "."},
    "limit": 3
  }'
```

Validation:

- `artifact_type` must be `dockerfile` or `compose`.

## Delete Cached Analysis

Endpoint: `DELETE /cache`

Purpose:

- Deletes one or more cache rows for a repository.

Example:

```bash
curl -X DELETE http://localhost:8080/cache \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/user/repo-name",
    "commit_sha": "abc123def456",
    "package_path": ".",
    "service_name": null
  }'
```

Behavior:

- `repo_url` is required.
- Add `commit_sha` to narrow deletion to a specific revision.
- Add `package_path` to narrow deletion to a specific monorepo scope.
- Add `service_name` to narrow deletion to a specific service cache row.
- If `service_name` is omitted or `null`, deletion targets rows where `service_name` is null.

## List Templates

Endpoint: `GET /templates`

Purpose:

- Lists Dockerfile templates from the template store.

Prerequisite:

- The `dockerfile_templates` table should exist in Supabase. The repository includes [`migrations/create_dockerfile_templates.sql`](/C:/Users/aniru/OneDrive/Desktop/own/sd-artifacts/migrations/create_dockerfile_templates.sql) for this.

Example:

```bash
curl "http://localhost:8080/templates?active_only=true"
```

Response shape:

```json
{
  "templates": [
    {
      "name": "nextjs_standalone",
      "description": "Next.js with standalone output (non-monorepo)",
      "match_stack_tokens": ["next"],
      "is_active": true
    }
  ]
}
```

## Create or Update Template

Endpoint: `POST /templates`

Purpose:

- Inserts or updates a template by name.

Example:

```bash
curl -X POST http://localhost:8080/templates \
  -H "Authorization: Bearer $API_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "custom_next_service",
    "description": "Custom Next.js Dockerfile",
    "match_stack_tokens": ["next", "pnpm"],
    "match_signals": {"has_standalone": true},
    "priority": 30,
    "template_content": "FROM node:20-alpine\nWORKDIR /app\n...",
    "variables": {"port": 3000},
    "is_active": true
  }'
```

## Seed Built-In Templates

Endpoint: `POST /templates/seed`

Purpose:

- Inserts or updates the built-in default template set in Supabase.

Prerequisite:

- The `dockerfile_templates` table must already exist.

Example:

```bash
curl -X POST http://localhost:8080/templates/seed \
  -H "Authorization: Bearer $API_BEARER_TOKEN"
```

## Delete Template

Endpoint: `DELETE /templates/{name}`

Purpose:

- Soft-deletes a template by marking it inactive.

Example:

```bash
curl -X DELETE http://localhost:8080/templates/custom_next_service \
  -H "Authorization: Bearer $API_BEARER_TOKEN"
```

## Response Contract Summary

Representative analysis fields:

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
