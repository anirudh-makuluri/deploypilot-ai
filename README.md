# SD-Artifacts Repo Analyzer

SD-Artifacts Repo Analyzer is an intelligent deployment companion that scans GitHub repositories and automatically prepares them for production deployment. Using a LangGraph-based workflow powered by Amazon Bedrock (Claude 3 Haiku), it analyzes the repository structure, detects the tech stack, infers required external services, and generates optimized, production-ready infrastructure files (Dockerfile, docker-compose.yml, nginx.conf).

## Features

- **Automated Repository Scanning:** Fetches file trees and key file contents (`package.json`, `requirements.txt`, etc.) from any public or private GitHub repository using your GitHub token.
- **Intelligent Stack Detection:** Automatically infers the application's tech stack, entry port, and needed external services (like databases, Redis, etc.).
- **Production-Ready Dockerfile Generation:** Creates multi-stage, secure, and optimized Dockerfiles for your application.
- **Docker Compose Setup:** Generates a `docker-compose.yml` file to handle your application and any required external dependencies for local or dev deployment.
- **Nginx Reverse Proxy Configuration:** Automatically produces an `nginx.conf` for a secure, production-grade reverse proxy.
- **RESTful API & Streaming:** Developed with FastAPI to provide a fast `/analyze` endpoint and a real-time `/analyze/stream` Server-Sent Events (SSE) endpoint for CI/CD or dashboard integration.

## Architecture

The AI reasoning engine uses a state-based workflow (LangGraph) to process repositories step-by-step:

```mermaid
graph TD
    Start(("Start")) --> scan["Scanner Node"]
    scan -->|Error| endNode(("End"))
    scan -->|Continue| plan["Planner Node"]
    
    plan -->|Error| endNode
    plan -->|Continue| build["Dockerfile Generator"]
    
    build --> compose["Compose Generator"]
    compose --> nginx["Nginx Generator"]
    nginx --> verify["Verifier Node"]
    verify --> endNode
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
   PORT=8080
   ```

## Usage

1. **Start the application:**
   ```bash
   python app.py
   # Or run directly with uvicorn
   # uvicorn app:app --host 0.0.0.0 --port 8080
   ```

2. **Analyze a Repository (Standard):**
   Send a `POST` request to the `/analyze` endpoint:
   
   ```bash
   curl -X POST http://localhost:8080/analyze \
        -H "Content-Type: application/json" \
        -d '{
              "repo_url": "https://github.com/user/repo-name",
              "github_token": "your_github_personal_access_token",
              "max_files": 50
            }'
   ```

3. **Analyze a Repository (Streaming):**
   For real-time progress, send a `POST` request to the `/analyze/stream` endpoint. It returns Server-Sent Events (SSE):
   
   ```bash
   # Use -N to keep the curl stream open
   curl -N -X POST http://localhost:8080/analyze/stream \
        -H "Content-Type: application/json" \
        -d '{
              "repo_url": "https://github.com/user/repo-name",
              "github_token": "your_github_personal_access_token"
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

## Tech Stack

- **FastAPI** & **Uvicorn**: High-performance REST API.
- **LangChain** & **LangGraph**: State-based agentic workflow design.
- **Amazon Bedrock (Claude 3 Haiku)**: The AI reasoning engine used for stack inference and infrastructure code generation.
- **GitHub API**: For repository file analysis.

## License

This project is open-source and available under the MIT License.
