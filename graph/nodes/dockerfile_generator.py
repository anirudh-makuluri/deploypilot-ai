from typing import Dict, Any
import json
from .llm_config import llm_docker, strip_markdown_wrapper


def dockerfile_generator_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Generate production Dockerfiles for each service."""
    scan = state.get("repo_scan", {})
    key_files = scan.get("key_files", {})
    services = state.get("services", [])
    
    dockerfiles = {}
    
    for service in services:
        svc_name = service["name"]
        build_ctx = service["build_context"]
        port = service["port"]
        dockerfile_path = service.get("dockerfile_path", "")
        
        # Look up the pre-existing Dockerfile using the planner-provided path
        existing_dockerfile = None
        if dockerfile_path:
            existing_dockerfile = key_files.get(dockerfile_path)
        
        if existing_dockerfile:
            prompt = f"""
You are a DevOps expert reviewing an existing Dockerfile.

Service: {svc_name}
Build context: {build_ctx}
Port: {port}
Stack: {state.get('detected_stack', 'unknown')}

EXISTING Dockerfile:
{existing_dockerfile}

Review this Dockerfile. If it follows production best practices (multi-stage builds, non-root user, slim images, proper EXPOSE/HEALTHCHECK), return it AS-IS.
If it can be improved, return the IMPROVED version.

Rules:
1. Use multi-stage builds if not already present.
2. Use slim/alpine base images.
3. Do NOT copy node_modules / venv directly, build inside builder stage.
4. Run as non-root user.
5. EXPOSE the correct port and add HEALTHCHECK.
6. Output ONLY Dockerfile content, no explanations. Do not wrap in markdown.
7. Do NOT include any preamble like 'IMPROVED Dockerfile:' or commentary. Return ONLY the raw Dockerfile.
"""
        else:
            prompt = f"""
Generate a PRODUCTION Dockerfile.

Service: {svc_name}
Build context: {build_ctx}
Port: {port}
Stack: {state.get('detected_stack', 'unknown')}
Repo scan: {json.dumps(scan, indent=2)}

Rules:
1. Use multi-stage builds.
2. Use slim/alpine base images.
3. Do NOT copy node_modules / venv directly from host, build inside the builder stage.
4. Run as non-root user.
5. EXPOSE the port and add HEALTHCHECK.
6. Output ONLY Dockerfile content, no explanations. Do not wrap in markdown.
7. Do NOT include any preamble or commentary. Return ONLY the raw Dockerfile.
"""
        
        resp = llm_docker.invoke(prompt)
        dockerfiles[svc_name] = strip_markdown_wrapper(resp.content)
    
    state["dockerfiles"] = dockerfiles
    return state
