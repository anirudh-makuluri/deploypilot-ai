# sd-artifacts — Planned Improvements

## [x] 1. Streaming Responses (SSE)
- Replace single POST response with Server-Sent Events
- Stream each node's output as it completes: scanner → planner → dockerfiles → compose → nginx → verifier
- Frontend gets real-time progress instead of waiting for the full pipeline
- Use FastAPI's `StreamingResponse` with `text/event-stream`

## 2. CI/CD Pipeline Generation
- New `cicd_generator_node` after the verifier
- Generate GitHub Actions workflow (`.github/workflows/deploy.yml`)
- Support GitLab CI (`.gitlab-ci.yml`) as an alternative
- Steps: build Docker images, push to registry, deploy to target (ECS, Cloud Run, k8s)
- Auto-detect existing CI configs and improve them (same pattern as Dockerfile handling)

## 3. Environment Variable Extraction
- New `env_extractor_node` that parses codebase for env var references
- Scan `process.env.*`, `os.getenv()`, `.env` files, and config files
- Generate a `.env.example` with all required variables, defaults, and descriptions
- Flag missing vars in compose and Dockerfiles

## 4. Cost Estimation
- Calculate `$` cost from token usage using model pricing
- Include `cost_usd` field in API response
- Support pricing for different models (Haiku, Sonnet, etc.)
- Track per-node cost breakdown for optimization

## 5. Kubernetes Manifest Generation
- New `k8s_generator_node` as an alternative to compose
- Generate: Deployment, Service, Ingress, ConfigMap, Secret templates
- Support Helm chart generation for parameterized deployments
- Add a request flag: `output_format: "compose" | "kubernetes" | "both"`

## [x] 6. Caching & Rate Limiting
- Cache repo scans by `repo_url + commit_sha` to avoid redundant GitHub API calls
- Use in-memory cache (TTL-based) or Redis for persistence
- Add rate limiting per API key / IP to prevent abuse
- Return cached results instantly when available

## 7. Deployment README Generation
- New `readme_generator_node` at the end of the pipeline
- Generate a `DEPLOY.md` with step-by-step deployment instructions
- Include: prerequisites, env setup, build commands, and common troubleshooting
- Tailored to the detected stack and generated configs

## 8. Retry Logic & Error Recovery
- Add exponential backoff retries for failed LLM calls
- Retry on malformed output (Pydantic validation failures)
- Set per-node timeout limits
- Fallback to simpler prompts on repeated failures

## 9. Neo4j Graph Database Integration
- Store repository analysis results as a knowledge graph
- **Node types**: Repository, Service, Dockerfile, ComposeFile, NginxConfig, EnvVar, Dependency
- **Relationships**: `HAS_SERVICE`, `USES_DOCKERFILE`, `DEPENDS_ON`, `EXPOSES_PORT`, `REQUIRES_ENV`
- **Use cases**:
  - Query similar repos: "Find all repos using Next.js + PostgreSQL"
  - Dependency graph visualization
  - Track analysis history and config drift over time
  - Recommend configurations based on similar stack patterns
  - Cross-repo insights: "Which repos use this same base image?"
- Add a `neo4j_storage_node` that persists analysis results after the verifier
- New API endpoints: `GET /repos/{id}/graph`, `GET /search?stack=nextjs`
