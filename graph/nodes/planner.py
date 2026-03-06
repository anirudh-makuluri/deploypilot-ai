from typing import Dict, Any, List
from pydantic import BaseModel, Field
import json
from .llm_config import llm_planner


class ServiceInfo(BaseModel):
    name: str = Field(description="Service name, e.g. 'frontend', 'websocket', 'api'")
    build_context: str = Field(description="Relative path to the service's build context, e.g. '.', './ws-server'")
    port: int = Field(description="The HTTP port the service listens on")
    dockerfile_path: str = Field(default="", description="Path to the existing Dockerfile for this service if one exists in key_files (e.g. 'Dockerfile', 'Dockerfile.websocket'). Empty string if no existing Dockerfile.")

class PlannerOutput(BaseModel):
    is_deployable: bool = Field(description="Whether this repo can be deployed as a web service. False for mobile apps, doc-only repos, CLI tools, etc.")
    error_reason: str = Field(default="", description="Why the repo is not deployable (empty string if deployable)")
    detected_stack: str = Field(description="Description of the tech stack, e.g. 'Next.js React app with WebSocket server'")
    services: List[ServiceInfo] = Field(description="List of services to build and deploy from this repo")
    has_existing_dockerfiles: bool = Field(description="Whether the repo already contains Dockerfile(s)")
    has_existing_compose: bool = Field(description="Whether the repo already contains a docker-compose.yml")


def planner_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Infer stack, services, and deployability from repo_scan using structured output."""
    scan = state.get("repo_scan", {})

    prompt = f"""
You are a DevOps architect analyzing a repository for deployment.

Given this repo scan:
{json.dumps(scan, indent=2)}

Tasks:
1. FIRST, determine if this repo is DEPLOYABLE as a web service:
   - Deployable: web apps, APIs, backend servers, full-stack apps
   - NOT deployable: mobile apps (React Native, Flutter, Swift, Kotlin), documentation-only repos, CLI tools, libraries/packages meant to be imported
   - If NOT deployable, set is_deployable=false and provide the reason.

2. If deployable, analyze the repo structure:
   - Identify ALL services that need to be built (e.g., a monorepo might have a frontend and a websocket server in separate directories)
   - For each service, determine its name, build context directory, and port
   - If the repo has existing Dockerfile(s) in key_files, map each Dockerfile to its corresponding service using the dockerfile_path field (e.g. 'Dockerfile' for the main app, 'Dockerfile.websocket' for the websocket service)
   - Check if the repo already has a docker-compose.yml/yaml in key_files

3. Describe the overall tech stack.

IMPORTANT: Look at the directory structure and key_files carefully. If there are multiple package.json or requirements.txt files in different directories, this is likely a monorepo with multiple services.
"""

    structured_llm = llm_planner.with_structured_output(PlannerOutput)
    
    try:
        data = structured_llm.invoke(prompt)
        
        if not data.is_deployable:
            state["error"] = data.error_reason or "This repository is not deployable as a web service"
            return state
        
        state["detected_stack"] = data.detected_stack
        state["services"] = [s.model_dump() for s in data.services]
        state["has_existing_dockerfiles"] = data.has_existing_dockerfiles
        state["has_existing_compose"] = data.has_existing_compose
    except Exception as e:
        state["error"] = f"Failed to analyze repository: {e}"
        
    return state
