# SD-Artifacts Repo Analyzer

SD-Artifacts Repo Analyzer is an intelligent deployment companion that scans GitHub repositories and automatically prepares them for production deployment. Using a LangGraph-based workflow powered by Amazon Bedrock (Claude 3 Haiku), it analyzes the repository structure, detects the tech stack, infers required external services, and generates optimized, production-ready infrastructure files (Dockerfile, docker-compose.yml, nginx.conf).

## Features

- **Automated Repository Scanning:** Fetches file trees and key file contents (`package.json`, `requirements.txt`, etc.) from any public or private GitHub repository using your GitHub token.
- **Intelligent Stack Detection:** Automatically infers the application's tech stack, entry port, and needed external services (like databases, Redis, etc.).
- **Production-Ready Dockerfile Generation:** Creates multi-stage, secure, and optimized Dockerfiles for your application.
- **Docker Compose Setup:** Generates a `docker-compose.yml` file to handle your application and any required external dependencies for local or dev deployment.
- **Nginx Reverse Proxy Configuration:** Automatically produces an `nginx.conf` for a secure, production-grade reverse proxy.
- **RESTful API & Streaming:** Developed with FastAPI to provide a fast `/analyze` endpoint and a real-time `/analyze/stream` Server-Sent Events (SSE) endpoint for CI/CD or dashboard integration.
- **Retry Logic & Error Recovery:** LLM-powered nodes use exponential backoff retries, timeout budgets, and fallback prompts for malformed or unstable model responses.

## Retry and Timeout Behavior

The planner, Dockerfile generator, compose generator, nginx generator, and verifier nodes all run through a shared retry wrapper.

- Retries: Exponential backoff with jitter for transient LLM failures.
- Validation recovery: Structured output and JSON parsing failures are retried.
- Timeout budget: Each node has a max time budget to avoid indefinite hangs.
- Prompt fallback: After repeated failures, each node switches to a simpler fallback prompt.

Current defaults are defined in `graph/nodes/llm_config.py` (`RETRY_CONFIGS` and `FALLBACK_PROMPTS`).

## Architecture

The AI reasoning engine uses a state-based workflow (LangGraph) to process repositories step-by-step:

```mermaid
graph TD
    Start(("Start")) --> scan["Scanner Node <br> (Checks Supabase Cache)"]
    scan -->|Cache Hit| endNode(("End (Return Cache)"))
    scan -->|Error| endNode
  scan -->|Cache Miss| planCall["Planner LLM Call"]

  planCall --> planRetry["Retry Wrapper <br> (exp backoff + jitter)"]
  planRetry --> planFallback["Fallback Prompt <br> (after repeated failures)"]
  planFallback --> planRetry
  planRetry -->|Success| planOK["Planner Output Validated"]
  planRetry -->|Timeout / Retries Exhausted| endNode

  planOK --> buildCall["Dockerfile Generator LLM Call"]
  buildCall --> buildRetry["Retry Wrapper"]
  buildRetry --> buildFallback["Fallback Prompt"]
  buildFallback --> buildRetry
  buildRetry -->|Success| composeCall["Compose Generator LLM Call"]
  buildRetry -->|Timeout / Retries Exhausted| endNode

  composeCall --> composeRetry["Retry Wrapper"]
  composeRetry --> composeFallback["Fallback Prompt"]
  composeFallback --> composeRetry
  composeRetry -->|Success| nginxCall["Nginx Generator LLM Call"]
  composeRetry -->|Timeout / Retries Exhausted| endNode

  nginxCall --> nginxRetry["Retry Wrapper"]
  nginxRetry --> nginxFallback["Fallback Prompt"]
  nginxFallback --> nginxRetry
  nginxRetry -->|Success| verifyCall["Verifier LLM Call"]
  nginxRetry -->|Timeout / Retries Exhausted| endNode

  verifyCall --> verifyRetry["Retry Wrapper"]
  verifyRetry --> verifyFallback["Fallback Prompt"]
  verifyFallback --> verifyRetry
  verifyRetry -->|Success| verifyDone["Verifier Node <br> (Saves to Cache)"]
  verifyRetry -->|Timeout / Retries Exhausted| endNode
  verifyDone --> endNode
```

## Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/anirudh-makuluri/sd-artifacts
   cd sd-artifacts
   ```

2. **Set up a virtual environment and install dependencies:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows use `venv\Scripts\activate`
   pip install -r requirements.txt
   ```

3. **Install Hadolint (Required for Verifier Node):**
   The Verifier node automatically runs `hadolint` to perform static code analysis on the generated Dockerfiles.
   - **macOS/Linux:** `brew install hadolint`
   - **Windows:** `scoop install hadolint`
   - Or download from [their releases page](https://github.com/hadolint/hadolint/releases).

3. **Configure Environment Variables:**
   Create a `.env` file in the root directory and add the following:
   ```env
   # Ensure you have your AWS access credentials configured for Amazon Bedrock
   AWS_ACCESS_KEY_ID=your_aws_access_key
   AWS_SECRET_ACCESS_KEY=your_aws_secret_key
   AWS_DEFAULT_REGION=your_aws_region
   BEDROCK_MODEL_ID=anthropic.claude-3-haiku-20240307-v1:0
  SUPABASE_URL=your_supabase_project_url
  SUPABASE_SERVICE_ROLE_KEY=your_supabase_service_role_key
   PORT=8080
   ```

4. **Initialize Supabase Tables:**
  Run `supabase_schema.sql` in your Supabase SQL editor to create:
  - `analysis_cache`
  - `example_bank` (for Dockerfile/compose reference examples)

## Usage

1. **Start the application:**
   ```bash
   python app.py
   # Or run directly with uvicorn
   # uvicorn app:app --host 0.0.0.0 --port 8080
   ```

2. **Analyze a Repository (Standard):**
  Send a `POST` request to the `/analyze` endpoint.
  `github_token` is optional for public repositories and required for private repositories:
   
   ```bash
   curl -X POST http://localhost:8080/analyze \
        -H "Content-Type: application/json" \
        -d '{
              "repo_url": "https://github.com/user/repo-name",
              "max_files": 50
            }'
   ```

3. **Analyze a Repository (Streaming):**
  For real-time progress, send a `POST` request to the `/analyze/stream` endpoint. It returns Server-Sent Events (SSE).
  `github_token` is optional for public repositories and required for private repositories:
   
   ```bash
   # Use -N to keep the curl stream open
   curl -N -X POST http://localhost:8080/analyze/stream \
        -H "Content-Type: application/json" \
        -d '{
            "repo_url": "https://github.com/user/repo-name"
            }'
   ```
   **Output Format:**
   ```text
   event: progress
   data: {"node": "scanner", "status": "completed"}
   
   event: progress
   data: {"node": "planner", "status": "completed"}
   ...
   event: complete
   data: { ... full JSON response ... }
   ```

4. **Response Structure:**
   The API will respond with JSON containing the detected stack, services needed, entry port, and all the generated infrastructure code (`dockerfile`, `docker_compose`, `nginx_conf`), alongside identified deployment risks and confidence score.

   **Example Response:**
   ```json
   {
     "commit_sha": "abc123def4567890",
     "stack_summary": "Next.js React app with WebSocket server",
     "services": [
       {
         "name": "app",
         "build_context": ".",
         "port": 3000,
         "dockerfile_path": "Dockerfile"
       },
       {
         "name": "websocket",
         "build_context": ".",
         "port": 4001,
         "dockerfile_path": "Dockerfile.websocket"
       }
     ],
     "dockerfiles": { ... },
     "docker_compose": "...",
     "nginx_conf": "...",
     "has_existing_dockerfiles": true,
     "has_existing_compose": true,
     "risks": [
       "- The Dockerfile for the \"app\" service does not pin the version of the apk packages installed..."
     ],
     "confidence": 0.8,
     "hadolint_results": {
       "app": "-:7 DL3018 warning: Pin versions in apk add. Instead of `apk add <package>` use `apk add <package>=<version>`",
       "websocket": ""
     },
     "token_usage": {
       "input_tokens": 11796,
       "output_tokens": 2507,
       "total_tokens": 14303
     }
   }
   ```

5. **Seed the Example Bank (Supabase):**
   Seed from your own curated list of repositories:
   ```bash
   curl -X POST http://localhost:8080/examples/seed \
        -H "Content-Type: application/json" \
        -d '{
              "repo_urls": [
                "https://github.com/vercel/next.js",
                "https://github.com/tiangolo/full-stack-fastapi-template"
              ],
              "max_files_per_repo": 20,
              "permissive_only": true
            }'
   ```

   Or seed from the built-in popular repository list:
   ```bash
   curl -X POST "http://localhost:8080/examples/seed/popular"
   ```

6. **Preview Retrieved Examples (Debug):**
   This lets you inspect what examples will be sent into generation prompts:
   ```bash
   curl -X POST http://localhost:8080/examples/preview \
        -H "Content-Type: application/json" \
        -d '{
              "artifact_type": "dockerfile",
              "detected_stack": "Next.js app with Node backend",
              "service": {"name": "web", "build_context": "."},
              "limit": 3
            }'
   ```

7. **Delete Cached Analysis Result:**
   Delete cached entries for a repository. Provide `commit_sha` to delete one specific cached result, or omit it to delete all cache rows for the repository.
   ```bash
   curl -X DELETE http://localhost:8080/cache \
        -H "Content-Type: application/json" \
        -d '{
              "repo_url": "https://github.com/user/repo-name",
              "commit_sha": "abc123def456"
            }'
   ```

## Tech Stack

- **FastAPI** & **Uvicorn**: High-performance REST API.
- **LangChain** & **LangGraph**: State-based agentic workflow design.
- **Amazon Bedrock (Claude 3 Haiku)**: The AI reasoning engine used for stack inference and infrastructure code generation.
- **GitHub API**: For repository file analysis.

## License

This project is open-source and available under the MIT License.
