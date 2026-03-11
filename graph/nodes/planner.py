from typing import Dict, Any, List
from pydantic import BaseModel, Field
import json
import os
from .llm_config import llm_planner, RETRY_CONFIGS, FALLBACK_PROMPTS
from graph.llm_retry import invoke_with_retry
from tools.port_and_stack_extractor import extract_port_and_stack
from tools.stack_tokens import (
    KNOWN_STACK_TOKENS,
    normalize_stack_tokens,
    render_stack_summary,
    unknown_stack_tokens,
)


class ServiceInfo(BaseModel):
    name: str = Field(description="Service name, e.g. 'frontend', 'websocket', 'api'")
    build_context: str = Field(description="Relative path to the service's build context, e.g. '.', './ws-server'")
    port: int = Field(description="The HTTP port the service listens on")
    dockerfile_path: str = Field(default="", description="Path to the existing Dockerfile for this service if one exists in key_files (e.g. 'Dockerfile', 'Dockerfile.websocket'). Empty string if no existing Dockerfile.")

class PlannerOutput(BaseModel):
    is_deployable: bool = Field(description="Whether this repo can be deployed as a web service. False for mobile apps, doc-only repos, CLI tools, etc.")
    error_reason: str = Field(default="", description="Why the repo is not deployable (empty string if deployable)")
    stack_tokens: List[str] = Field(description="Canonical stack tokens selected only from the allowed registry")
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


def _is_mobile_package_path(package_path: str) -> bool:
    normalized = _normalize_ctx(package_path).lower()
    tokens = set(filter(None, normalized.split("/")))
    mobile_tokens = {"mobile", "android", "ios", "react-native", "reactnative", "expo", "flutter"}
    return any(token in mobile_tokens for token in tokens)


def _fallback_service_name(package_path: str, stack_tokens: List[str]) -> str:
    token_set = {token.lower() for token in stack_tokens}
    lower_path = _normalize_ctx(package_path).lower()
    if "backend" in lower_path or "api" in lower_path or {"fastapi", "flask", "django", "uvicorn", "gunicorn"}.intersection(token_set):
        return "api"
    if "next-app" in lower_path or "web" in lower_path:
        return "web"
    if "frontend" in lower_path or "web" in lower_path or {"next", "react", "vue", "svelte", "angular", "vite", "nginx"}.intersection(token_set):
        return "web"
    return "app"


def _choose_fallback_build_context(scan: Dict[str, Any], package_path: str) -> str:
    """Prefer a nested app directory when package_path is a container folder."""
    package_norm = _normalize_ctx(package_path)
    dirs = scan.get("dirs", [])
    if not isinstance(dirs, list) or not dirs:
        return package_norm

    child_dirs = []
    prefix = package_norm + "/"
    for raw_dir in dirs:
        normalized_dir = _normalize_ctx(str(raw_dir))
        if not normalized_dir.startswith(prefix):
            continue
        relative = normalized_dir[len(prefix):]
        if not relative or "/" in relative:
            continue
        child_dirs.append(normalized_dir)

    if not child_dirs:
        return package_norm

    preferred_markers = ["next-app", "web", "frontend", "site", "client"]
    for marker in preferred_markers:
        for child in child_dirs:
            if marker in child.lower().split("/")[-1]:
                return child

    return package_norm


def _scan_has_existing_dockerfiles(scan: Dict[str, Any]) -> bool:
    key_files = scan.get("key_files", {})
    if not isinstance(key_files, dict):
        return False
    for path in key_files.keys():
        lower_name = str(path).replace("\\", "/").split("/")[-1].lower()
        if lower_name == "dockerfile" or lower_name.startswith("dockerfile.") or lower_name.endswith(".dockerfile"):
            return True
    return False


def _scan_has_existing_compose(scan: Dict[str, Any]) -> bool:
    key_files = scan.get("key_files", {})
    if not isinstance(key_files, dict):
        return False
    compose_names = {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}
    for path in key_files.keys():
        lower_name = str(path).replace("\\", "/").split("/")[-1].lower()
        if lower_name in compose_names:
            return True
    return False


def _apply_deterministic_fallback(
    state: Dict[str, Any],
    scan: Dict[str, Any],
    package_path: str,
    extraction_result: Dict[str, Any],
    llm_stack_tokens: List[str] | None = None,
) -> bool:
    if not extraction_result.get("success"):
        return False
    if _is_mobile_package_path(package_path):
        return False

    fallback_build_context = _choose_fallback_build_context(scan, package_path)
    fallback_port = int(extraction_result.get("port") or 3000)
    probe_service = ServiceInfo(
        name="fallback",
        build_context=fallback_build_context,
        port=fallback_port,
        dockerfile_path="",
    )
    if _is_mobile_service(probe_service, scan):
        return False

    extracted_stack_tokens = extraction_result.get("stack_tokens", []) or []

    # If we selected a nested child context and the parent extraction is sparse,
    # retry deterministic extraction at the chosen child for better stack fidelity.
    if not extracted_stack_tokens and fallback_build_context != _normalize_ctx(package_path):
        repo_url = state.get("repo_url", "")
        if repo_url:
            try:
                refined = extract_port_and_stack(
                    repo_url,
                    build_context=fallback_build_context,
                    timeout=30,
                    github_token=os.getenv("GITHUB_TOKEN"),
                )
                if refined.get("success"):
                    extracted_stack_tokens = refined.get("stack_tokens", []) or extracted_stack_tokens
            except Exception:
                pass
    combined_stack_tokens = normalize_stack_tokens([*(llm_stack_tokens or []), *extracted_stack_tokens])
    fallback_name = _fallback_service_name(fallback_build_context, combined_stack_tokens)

    state["stack_tokens"] = combined_stack_tokens
    state["detected_stack"] = render_stack_summary(combined_stack_tokens)
    state["services"] = [
        ServiceInfo(
            name=fallback_name,
            build_context=fallback_build_context,
            port=fallback_port,
            dockerfile_path="",
        ).model_dump()
    ]
    state["has_existing_dockerfiles"] = _scan_has_existing_dockerfiles(scan)
    state["has_existing_compose"] = _scan_has_existing_compose(scan)
    state["planner_used_deterministic_fallback"] = True
    state["extraction_result"] = extraction_result
    return True


def planner_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Infer stack, services, and deployability from repo_scan using structured output."""
    scan = state.get("repo_scan", {})
    repo_url = state.get("repo_url", "")
    package_path = state.get("package_path", ".")
    
    # Extract ports and stack tokens from cloned repo for deterministic signal
    extraction_result = {}
    if repo_url:
        try:
            github_token = os.getenv("GITHUB_TOKEN")
            extraction_result = extract_port_and_stack(
                repo_url,
                build_context=package_path,
                timeout=30,
                github_token=github_token
            )
        except Exception as e:
            print(f"Warning: Port/stack extraction failed (proceeding with LLM only): {e}")
            extraction_result = {"success": False, "error": str(e)}
    
    extracted_port = extraction_result.get("port")
    extracted_stack_tokens = extraction_result.get("stack_tokens", [])
    extraction_context = ""
    if extraction_result.get("success") and extracted_stack_tokens:
        extraction_context = f"""
DETERMINISTIC STACK SIGNAL (from cloned repo analysis):
- Detected runtimes/frameworks: {', '.join(extracted_stack_tokens)}
Prefer these tokens unless the repo scan clearly supports additional allowed tokens.
"""

    allowed_stack_tokens = ", ".join(sorted(KNOWN_STACK_TOKENS))

    prompt = f"""
You are a DevOps architect analyzing a repository for deployment.

{extraction_context}

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

3. Return stack_tokens using ONLY canonical tokens from this allowed set:
    {allowed_stack_tokens}

4. Do not invent frameworks or tools. If a token is not strongly supported by repo evidence, leave it out.

IMPORTANT: Look at the directory structure and key_files carefully. If there are multiple package.json or requirements.txt files in different directories, this is likely a monorepo with multiple services.
"""

    prompt += """
Respond ONLY with a raw JSON object matching this schema. Do not include markdown code block wrappers or any explanations:
{
  "is_deployable": boolean,
  "error_reason": "string (empty if deployable)",
    "stack_tokens": ["string"],
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

    def _invoke(raw_prompt: str):
        return llm_planner.invoke(raw_prompt)

    def _validate(response):
        content = response.content.strip()
        if content.startswith("```"):
            import re

            content = re.sub(r"^```(?:json)?\s*\n(.*?)\n```$", r"\1", content, flags=re.DOTALL)
        json_data = json.loads(content)
        raw_tokens = json_data.get("stack_tokens") or []
        if not isinstance(raw_tokens, list):
            raise ValueError("stack_tokens must be a list")
        invalid_tokens = unknown_stack_tokens(raw_tokens)
        if invalid_tokens:
            raise ValueError(f"Unknown stack tokens: {', '.join(invalid_tokens)}")
        json_data["stack_tokens"] = normalize_stack_tokens(raw_tokens)
        return PlannerOutput(**json_data)

    try:
        data, attempts_used, fallback_used = invoke_with_retry(
            invoke_fn=_invoke,
            prompt=prompt,
            validator=_validate,
            fallback_prompt=FALLBACK_PROMPTS["planner"],
            config=RETRY_CONFIGS["planner"],
            node_name="planner",
        )
        
        if not data.is_deployable:
            state["error"] = data.error_reason or "This repository is not deployable as a web service"
            return state

        filtered_services = [s for s in data.services if not _is_mobile_service(s, scan)]
        if not filtered_services:
            if _apply_deterministic_fallback(
                state=state,
                scan=scan,
                package_path=package_path,
                extraction_result=extraction_result,
                llm_stack_tokens=data.stack_tokens,
            ):
                state["planner_retry_attempts"] = attempts_used
                state["planner_fallback_used"] = fallback_used
                return state
            else:
                state["error"] = "No web-deployable services found. Repository appears to contain only mobile or non-deployable packages."
                return state
        
        # Carefully override ports only for single-service repos with high-confidence extraction
        if len(filtered_services) == 1 and extracted_port and extraction_result.get("port_confidence", 0) >= 0.65:
            filtered_services[0].port = extracted_port
        
        combined_stack_tokens = normalize_stack_tokens([*data.stack_tokens, *extracted_stack_tokens])

        state["stack_tokens"] = combined_stack_tokens
        state["detected_stack"] = render_stack_summary(combined_stack_tokens)
        state["services"] = [s.model_dump() for s in filtered_services]
        state["has_existing_dockerfiles"] = data.has_existing_dockerfiles
        state["has_existing_compose"] = data.has_existing_compose
        state["planner_retry_attempts"] = attempts_used
        state["planner_fallback_used"] = fallback_used
        state["extraction_result"] = extraction_result
    except Exception as e:
        if _apply_deterministic_fallback(
            state=state,
            scan=scan,
            package_path=package_path,
            extraction_result=extraction_result,
            llm_stack_tokens=[],
        ):
            return state

        error_details = str(e)
        state["error"] = f"Failed to analyze repository: {error_details}"
        
        
    return state
