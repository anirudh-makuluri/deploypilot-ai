from typing import Dict, Any, List
from langchain_aws import ChatBedrock
from tools.github_tools import fetch_repo_structure
import json
import os
import time
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

# Initialize LLMs (Using Amazon Bedrock)
# Ensure AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, and AWS_DEFAULT_REGION are in your .env
llm_scanner = ChatBedrock(
    model_id=os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0"),
    model_kwargs={"temperature": 0.1}
)
scanner_model = llm_scanner.bind_tools([fetch_repo_structure])

llm_planner = ChatBedrock(
    model_id=os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0"),
    model_kwargs={"temperature": 0.1}
)
llm_docker = ChatBedrock(
    model_id=os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0"),
    model_kwargs={"temperature": 0.0}
)
llm_compose = ChatBedrock(
    model_id=os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0"),
    model_kwargs={"temperature": 0.0}
)
llm_nginx = ChatBedrock(
    model_id=os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0"),
    model_kwargs={"temperature": 0.0}
)

def scanner_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Calls GitHub tool directly to populate repo_scan."""
    
    scan = fetch_repo_structure.invoke({
        "repo_url": state["repo_url"],
        "github_token": state["github_token"],
        "max_files": state.get("max_files", 50)
    })
    
    state["repo_scan"] = scan
    return state

class PlannerOutput(BaseModel):
    detected_stack: str = Field(description="Describe the stack, e.g. 'Node.js Express API with Postgres'")
    services_needed: List[str] = Field(description="List of external services needed, e.g. ['postgres']")
    entry_port: int = Field(description="The primary HTTP port (guess 3000 for Node, 8000 for Python if unclear)")

def planner_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Infer stack, services, and port from repo_scan using structured output."""
    scan = state.get("repo_scan", {})

    prompt = f"""
You are a DevOps architect.

Given this repo scan:
{scan}

Task:
1. Describe the stack, e.g. "Node.js Express API with Postgres".
2. Decide which external services are needed as a JSON list, e.g. ["postgres"].
3. Infer the primary HTTP port (guess 3000 for Node, 8000 for Python if unclear).
"""

    structured_llm = llm_planner.with_structured_output(PlannerOutput)
    
    try:
        data = structured_llm.invoke(prompt)
        state["detected_stack"] = data.detected_stack
        state["services_needed"] = data.services_needed
        state["entry_port"] = data.entry_port
    except Exception as e:
        state["detected_stack"] = "unknown"
        state["services_needed"] = []
        state["entry_port"] = 3000
        print(f"Failed to parse planner JSON: {e}")
        
    return state


def dockerfile_generator_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Generate a production Dockerfile from scan + plan."""
    print("=" * 50)
    print("dockerfile_generator_node")
    print(state)
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
3. Do NOT copy node_modules / venv directly from host to container, build inside the builder stage.
4. Run as non-root user.
5. EXPOSE the port and add HEALTHCHECK.
6. Output ONLY Dockerfile content, no explanations. Do not wrap in markdown tags like ```docker.
"""

    resp = llm_docker.invoke(prompt)
    dockerfile = resp.content.strip("`").strip()
    # Handle the ```docker formatting if the model still uses it
    if dockerfile.startswith("docker\n"):
         dockerfile = dockerfile[7:]
    
    state["dockerfile"] = dockerfile
    # simple confidence heuristic for now
    state["confidence"] = 0.9
    
    return state


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
- Add postgres service if 'postgres' in services_needed. Add redis if 'redis', etc.
- Use default local credentials (NOTE: user will change in production).
- Output ONLY YAML, no markdown wrappers like ```yaml.
"""

    resp = llm_compose.invoke(prompt)
    compose = resp.content.strip("`").strip()
    if compose.startswith("yaml\n"):
        compose = compose[5:]
    state["docker_compose"] = compose
    
    return state
    
def nginx_generator_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Generate an nginx.conf for production deployment."""
    prompt = f"""
Generate a production nginx.conf reverse proxy configuration.

INPUT:
- App port: {state.get('entry_port', 3000)}

Rules:
- Listen on port 80 (since SmartDeploy standardizes edge).
- Proxy_pass to http://app:{state.get('entry_port', 3000)}.
- Include standard security headers (X-Frame-Options, X-Content-Type-Options, etc.).
- Include proper proxy headers (X-Real-IP, X-Forwarded-For).
- Output ONLY nginx configs, no markdown wrappers.
"""

    resp = llm_nginx.invoke(prompt)
    nginx_conf = resp.content.strip("`").strip()
    if nginx_conf.startswith("nginx\n"):
        nginx_conf = nginx_conf[6:]
    if nginx_conf.startswith("conf\n"):
        nginx_conf = nginx_conf[5:]
    state["nginx_conf"] = nginx_conf
    
    return state
