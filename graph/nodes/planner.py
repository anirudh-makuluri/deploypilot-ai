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


def _normalize_ctx(path: str) -> str:
    """Normalize build context paths for reliable comparisons."""
    if not path:
        return "."
    normalized = path.replace("\\", "/").strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized or "."


def _is_mobile_service(service: ServiceInfo, scan: Dict[str, Any]) -> bool:
    """Return True when a service appears to be mobile-only (React Native/Expo/Flutter/iOS/Android)."""
    name = (service.name or "").lower()
    ctx = _normalize_ctx(service.build_context).lower()

    mobile_markers = {
        "mobile",
        "android",
        "ios",
        "react-native",
        "reactnative",
        "expo",
        "flutter",
    }

    ctx_tokens = set(filter(None, ctx.split("/")))
    if any(marker in name for marker in mobile_markers):
        return True
    if any(marker in ctx_tokens for marker in mobile_markers):
        return True

    key_files = scan.get("key_files", {})
    dirs = [d.replace("\\", "/").lower() for d in scan.get("dirs", [])]

    # Use the package.json that belongs to this build context when available.
    candidate_package_paths = []
    if ctx == ".":
        candidate_package_paths.append("package.json")
    else:
        candidate_package_paths.append(f"{ctx}/package.json")

    package_content = ""
    for pkg_path in candidate_package_paths:
        if pkg_path in key_files:
            package_content = key_files[pkg_path].lower()
            break

    package_mobile_markers = [
        '"react-native"',
        '"expo"',
        '"@react-native',
        '"metro"',
        '"detox"',
        '"eas-cli"',
    ]
    if package_content and any(marker in package_content for marker in package_mobile_markers):
        return True

    # Additional path-level signal for native mobile projects.
    if ctx != ".":
        android_dir = f"{ctx}/android"
        ios_dir = f"{ctx}/ios"
        if android_dir in dirs or ios_dir in dirs:
            return True

    return False


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
    - Identify ONLY web-deployable services that need to be built (e.g., a monorepo might have a frontend and a websocket server in separate directories)
    - EXCLUDE mobile app packages from services even if they exist in the same monorepo (React Native/Expo/Flutter/iOS/Android apps should never get Dockerfiles here)
   - For each service, determine its name, build context directory, and port
   - If the repo has existing Dockerfile(s) in key_files, map each Dockerfile to its corresponding service using the dockerfile_path field (e.g. 'Dockerfile' for the main app, 'Dockerfile.websocket' for the websocket service)
   - Check if the repo already has a docker-compose.yml/yaml in key_files

3. Describe the overall tech stack.

IMPORTANT: Look at the directory structure and key_files carefully. If there are multiple package.json or requirements.txt files in different directories, this is likely a monorepo with multiple services.
"""

    prompt += """
Respond ONLY with a raw JSON object matching this schema. Do not include markdown code block wrappers or any explanations:
{
  "is_deployable": boolean,
  "error_reason": "string (empty if deployable)",
  "detected_stack": "string",
  "services": [
    {
      "name": "string",
      "build_context": "string",
      "port": integer,
      "dockerfile_path": "string"
    }
  ],
  "has_existing_dockerfiles": boolean,
  "has_existing_compose": boolean
}
"""

    try:
        # First try parsing it manually
        response = llm_planner.invoke(prompt)
        content = response.content.strip()
        
        # Strip markdown if the LLM still added it
        if content.startswith("```"):
            import re
            content = re.sub(r"^```(?:json)?\s*\n(.*?)\n```$", r"\1", content, flags=re.DOTALL)
            
        json_data = json.loads(content)
        data = PlannerOutput(**json_data)
        
        if not data.is_deployable:
            state["error"] = data.error_reason or "This repository is not deployable as a web service"
            return state

        filtered_services = [s for s in data.services if not _is_mobile_service(s, scan)]
        if not filtered_services:
            state["error"] = "No web-deployable services found. Repository appears to contain only mobile or non-deployable packages."
            return state
        
        state["detected_stack"] = data.detected_stack
        state["services"] = [s.model_dump() for s in filtered_services]
        state["has_existing_dockerfiles"] = data.has_existing_dockerfiles
        state["has_existing_compose"] = data.has_existing_compose
    except Exception as e:
        # We also grab the raw message to figure out what it generated before failing
        import traceback
        error_details = str(e)
        state["error"] = f"Failed to analyze repository: {error_details}"
        
        
    return state
