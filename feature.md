Here’s a detailed `README.md` you can hand directly to your coding agent.

***

# Repo Analyzer Agent for SmartDeploy

## Goal

Build a **separate analyzer service** that SmartDeploy can call to:

1. Inspect a GitHub repository.
2. Infer the tech stack and deployment requirements.
3. Generate a **production-ready Dockerfile** for single-service apps.
4. Optionally generate a **docker-compose.yml** for multi-service setups (app + DB, etc.).
5. Return a structured JSON response that SmartDeploy’s frontend can render.

This service is **read-only** with respect to user repos (no auto-commits). It should be easy to deploy independently (e.g., Render / Cloud Run) and called via HTTP.

***

## High-level Architecture

### Components

- **Frontend**: Existing SmartDeploy Next.js UI (unchanged, except for new “Analyze repo” button).
- **Analyzer API**: New Python service:
  - Framework: **FastAPI**.
  - Orchestration: **LangGraph** (Python).
  - LLM: **Google Gemini** via `langchain_google_genai`.
- **GitHub integration**:
  - Uses user’s existing GitHub OAuth token (passed from SmartDeploy frontend).
  - Calls GitHub REST API to fetch repo tree and key files.

### Request / Response

**FastAPI endpoint:**

- `POST /analyze`
- Request body:
  ```json
  {
    "repo_url": "https://github.com/user/repo",
    "github_token": "ghp_xxx",
    "max_files": 50
  }
  ```
- Response body:
  ```json
  {
    "stack_summary": "Node.js Express API with Postgres",
    "services_needed": ["postgres"],
    "entry_port": 3000,
    "dockerfile": "FROM node:20-alpine ...",
    "docker_compose": "version: '3.9' ...",  // may be null
    "risks": [
      "No healthcheck defined",
      "Runs as root user"
    ],
    "confidence": 0.9
  }
  ```

***

## LangGraph Design

We use a **multi-agent graph** with deterministic routing.

### State Schema

Represent state as a Python dict (later can be a TypedDict / Pydantic model):

```python
State = {
  "repo_url": str,
  "github_token": str,
  "max_files": int,
  "repo_scan": dict,          # raw GitHub scan data
  "detected_stack": str,      # "Node.js Express API"
  "services_needed": list,    # ["postgres"]
  "entry_port": int,          # 3000
  "dockerfile": str | None,
  "docker_compose": str | None,
  "risks": list,              # ["No healthcheck", ...]
  "confidence": float | None
}
```

### Nodes (Agents)

1. **scanner_node**
   - Inputs: `repo_url`, `github_token`, `max_files`.
   - Calls GitHub tools to:
     - Fetch repo metadata (language, stars).
     - Fetch file tree (limited by `max_files`).
     - Read key files: `package.json`, `requirements.txt`, `Dockerfile`, `docker-compose.yml`, `pnpm-lock.yaml`, etc.
   - Output: sets `state["repo_scan"]`.

2. **planner_node**
   - Inputs: `repo_scan`.
   - Uses Gemini to infer:
     - `detected_stack` (e.g., "Next.js app with Node API").
     - `services_needed` (e.g., `["postgres"]` if `pg` detected).
     - `entry_port` (e.g., 3000).
   - Output: updates `detected_stack`, `services_needed`, `entry_port`.

3. **dockerfile_generator_node**
   - Inputs: `repo_scan`, `detected_stack`, `services_needed`, `entry_port`.
   - Uses Gemini to generate a **Dockerfile** following best practices.
   - Optionally passes result through a lint/validator tool.
   - Output: sets `dockerfile` and `confidence` (basic confidence score).

4. **compose_generator_node** (optional for v1, but plan it)
   - Inputs: `services_needed`, `dockerfile`, `entry_port`.
   - Generates a minimal `docker-compose.yml` when additional services are needed (e.g., Postgres).
   - Output: sets `docker_compose`.

5. **validator_node** (optional in v1, can be folded into generator)
   - Inputs: `dockerfile`, `docker_compose`.
   - Performs basic static checks (via tool) and populates `risks` and potentially adjusts `confidence`.

### Graph Edges

Deterministic routing (no LLM routing):

```python
start → scanner → planner → dockerfile_generator → validator → END

# Conditional fork for compose:
planner → compose_generator (if services_needed non-empty)
compose_generator → dockerfile_generator
```

In code, use:

- `workflow.add_node(name, fn)`
- `workflow.add_edge("scanner", "planner")`, etc.
- `workflow.add_conditional_edges("planner", condition_fn, {"compose": "compose_generator", "no_compose": "dockerfile_generator"})`.

***

## Tools

### 1. GitHub Repo Scanner Tool

**File**: `tools/github_tools.py`

Purpose: wrap GitHub API access in a LangChain tool usable by the scanner node.

```python
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from typing import Optional
import requests

class RepoScanInput(BaseModel):
    repo_url: str = Field(..., description="Full GitHub repo URL")
    github_token: str = Field(..., description="User's GitHub token")
    max_files: Optional[int] = Field(20, description="Max files to analyze")

@tool(args_schema=RepoScanInput)
def fetch_repo_structure(input: RepoScanInput) -> dict:
    """Fetch repo metadata, file tree, and key file contents for deploy analysis."""
    repo = input.repo_url.split("github.com/") [github](https://github.com/langchain-ai/langgraph/blob/main/docs/docs/concepts/template_applications.md).rstrip("/")

    headers = {"Authorization": f"token {input.github_token}"}

    meta = requests.get(f"https://api.github.com/repos/{repo}", headers=headers).json()

    tree = requests.get(
        f"https://api.github.com/repos/{repo}/git/trees/{meta['default_branch']}?recursive=1",
        headers=headers,
    ).json()

    key_paths = [
        "package.json",
        "requirements.txt",
        "pnpm-lock.yaml",
        "Dockerfile",
        "docker-compose.yml",
    ]
    key_files = {}

    count = 0
    for item in tree.get("tree", []):
        if count >= input.max_files:
            break
        if item["type"] == "blob" and item["path"] in key_paths:
            content_url = f"https://raw.githubusercontent.com/{repo}/{meta['default_branch']}/{item['path']}"
            key_files[item["path"]] = requests.get(content_url, headers=headers).text[:4000]
            count += 1

    result = {
        "repo_full_name": meta["full_name"],
        "default_branch": meta["default_branch"],
        "language": meta.get("language"),
        "stargazers_count": meta.get("stargazers_count", 0),
        "key_files": key_files,
        "dirs": [i["path"] for i in tree.get("tree", []) if i["type"] == "tree"][:20],
    }
    return result
```

***

## Node Implementations

### 1. Scanner Node

**File**: `graph/nodes.py`

```python
from typing import Dict, Any
from langchain_google_genai import ChatGoogleGenerativeAI
from tools.github_tools import fetch_repo_structure

llm_scanner = ChatGoogleGenerativeAI(model="gemini-2.0-flash-exp", temperature=0.1)
scanner_model = llm_scanner.bind_tools([fetch_repo_structure])

def scanner_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Calls GitHub tool via Gemini to populate repo_scan."""
    prompt = f"""
You are a repo scanner preparing for deployment.

Goal:
- Use the `fetch_repo_structure` tool to inspect this GitHub repo.
- Do NOT try to infer stack manually, just call the tool.

Repo URL: {state['repo_url']}
"""

    resp = scanner_model.invoke(prompt)

    if resp.tool_calls:
        # single tool call expected
        tool_call = resp.tool_calls[0]
        tool_args = tool_call["args"]
        # enforce token and max_files from state
        tool_args.setdefault("github_token", state["github_token"])
        tool_args.setdefault("max_files", state.get("max_files", 50))
        scan = fetch_repo_structure.invoke(tool_args)
        state["repo_scan"] = scan
    else:
        # fallback: no tool call
        state["repo_scan"] = {}

    return state
```

### 2. Planner Node

```python
llm_planner = ChatGoogleGenerativeAI(model="gemini-2.0-pro-exp", temperature=0.1)

def planner_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Infer stack, services, and port from repo_scan."""
    scan = state.get("repo_scan", {})

    prompt = f"""
You are a DevOps architect.

Given this repo scan:
{scan}

Task:
1. Describe the stack, e.g. "Node.js Express API with Postgres".
2. Decide which external services are needed as a JSON list, e.g. ["postgres"].
3. Infer the primary HTTP port (guess 3000 for Node, 8000 for Python if unclear).

Return JSON:
{{
  "detected_stack": "...",
  "services_needed": ["postgres"],
  "entry_port": 3000
}}
"""

    resp = llm_planner.invoke(prompt)
    # Assume model returns JSON in content
    import json
    data = json.loads(resp.content)

    state["detected_stack"] = data.get("detected_stack", "unknown")
    state["services_needed"] = data.get("services_needed", [])
    state["entry_port"] = data.get("entry_port", 3000)
    return state
```

### 3. Dockerfile Generator Node

Use the code pattern we discussed earlier:

```python
llm_docker = ChatGoogleGenerativeAI(model="gemini-2.0-flash-exp", temperature=0.0)

def dockerfile_generator_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Generate a production Dockerfile from scan + plan."""
    prompt = f"""
Generate a PRODUCTION Dockerfile.

INPUT:
- Repo scan: {state.get('repo_scan', {})}
- Stack: {state.get('detected_stack', 'unknown')}
- Services: {state.get('services_needed', [])}
- Port: {state.get('entry_port', 3000)}

Rules:
1. Use multi-stage builds.
2. Use slim/alpine base images.
3. Do NOT copy node_modules / venv.
4. Run as non-root user.
5. EXPOSE the port and add HEALTHCHECK.
6. Output ONLY Dockerfile content, no explanations.
"""

    resp = llm_docker.invoke(prompt)
    dockerfile = resp.content.strip("`").strip()
    state["dockerfile"] = dockerfile
    # simple confidence heuristic for now
    state["confidence"] = 0.9
    return state
```

### 4. (Optional) Compose Generator Node

```python
llm_compose = ChatGoogleGenerativeAI(model="gemini-2.0-flash-exp", temperature=0.0)

def compose_generator_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Generate a docker-compose.yml if services_needed is non-empty."""
    if not state.get("services_needed"):
        state["docker_compose"] = None
        return state

    prompt = f"""
Generate a docker-compose.yml for local/dev deployment.

INPUT:
- Services: {state['services_needed']}
- App port: {state['entry_port']}

Rules:
- One service called 'app' building from local Dockerfile.
- Add postgres service if 'postgres' in services_needed.
- Use default local credentials (NOTE: user will change in production).
- Output ONLY YAML.
"""

    resp = llm_compose.invoke(prompt)
    compose = resp.content.strip("`").strip()
    state["docker_compose"] = compose
    return state
```

***

## Graph Wiring

**File**: `graph/graph.py`

```python
from langgraph.graph import StateGraph, END

from .nodes import (
    scanner_node,
    planner_node,
    dockerfile_generator_node,
    compose_generator_node,
)

# State is a plain dict
workflow = StateGraph(dict)

workflow.add_node("scanner", scanner_node)
workflow.add_node("planner", planner_node)
workflow.add_node("docker_gen", dockerfile_generator_node)
workflow.add_node("compose_gen", compose_generator_node)

# Edges
workflow.add_edge("scanner", "planner")

def needs_compose(state):
    return "compose" if state.get("services_needed") else "no_compose"

workflow.add_conditional_edges(
    "planner",
    needs_compose,
    {
        "compose": "compose_gen",
        "no_compose": "docker_gen",
    },
)

workflow.add_edge("compose_gen", "docker_gen")
workflow.add_edge("docker_gen", END)

# Entry point
workflow.set_entry_point("scanner")

graph = workflow.compile()
```

***

## FastAPI Service

**File**: `app.py`

```python
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional
from graph.graph import graph

app = FastAPI()

class AnalyzeRequest(BaseModel):
    repo_url: str
    github_token: str
    max_files: Optional[int] = 50

@app.post("/analyze")
async def analyze_repo(req: AnalyzeRequest):
    initial_state = {
        "repo_url": req.repo_url,
        "github_token": req.github_token,
        "max_files": req.max_files,
    }
    result = graph.invoke(initial_state)
    # Filter to keys we want to expose
    return {
        "stack_summary": result.get("detected_stack"),
        "services_needed": result.get("services_needed", []),
        "entry_port": result.get("entry_port"),
        "dockerfile": result.get("dockerfile"),
        "docker_compose": result.get("docker_compose"),
        "risks": result.get("risks", []),
        "confidence": result.get("confidence", 0.0),
    }
```

***

## Frontend (SmartDeploy) Integration

From the Next.js app:

- Add a button: **“Analyze repo”**.
- On click:

```ts
const analyzeRepo = async () => {
  const res = await fetch("https://ANALYZER_SERVICE_URL/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      repo_url: selectedRepoUrl,
      github_token: userGithubToken,
      max_files: 50,
    }),
  });

  const data = await res.json();
  // Render data.dockerfile, data.docker_compose, data.stack_summary, etc.
};
```

***

## Non-goals / Constraints

- Do **not** mutate or commit to the user’s GitHub repo in v1.
- Do **not** integrate directly into existing SmartDeploy backend; keep this as a separate service reachable via HTTP.
- This agent does not perform actual deployment; it only proposes containerization configs.

***

If anything is ambiguous, default to:

- Keep the code small and readable.
- Favor **deterministic routing** (edges) over clever LLM-based routing.
- Use **Gemini 2.x Flash** for speed; Pro only where necessary.