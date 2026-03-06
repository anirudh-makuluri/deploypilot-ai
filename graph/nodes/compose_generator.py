from typing import Dict, Any
import json
from .llm_config import llm_compose, strip_markdown_wrapper


def compose_generator_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Generate a docker-compose.yml for all services."""
    scan = state.get("repo_scan", {})
    key_files = scan.get("key_files", {})
    services = state.get("services", [])
    
    # Check for existing docker-compose
    existing_compose = None
    for path, content in key_files.items():
        filename = path.split("/")[-1]
        if filename in ("docker-compose.yml", "docker-compose.yaml"):
            existing_compose = content
            break
    
    services_desc = "\n".join([
        f"  - {s['name']}: build context={s['build_context']}, port={s['port']}"
        for s in services
    ])
    
    if existing_compose:
        prompt = f"""
You are a DevOps expert reviewing an existing docker-compose.yml.

Services in this repo:
{services_desc}

Stack: {state.get('detected_stack', 'unknown')}

EXISTING docker-compose.yml:
{existing_compose}

Review this docker-compose. If it correctly maps all services with proper build contexts, ports, volumes, and includes any needed external services (databases, caches, etc.), return it AS-IS.
If it can be improved (missing services, wrong ports, missing health checks, etc.), return the IMPROVED version.

Rules:
- Each app service should build from its respective directory with the correct Dockerfile.
- Add external services (postgres, redis, etc.) if the codebase references them but they're missing.
- Use environment variables for credentials (with placeholder values).
- Output ONLY YAML, no markdown wrappers.
- Do NOT include any explanations, analysis, or commentary. Return ONLY the raw YAML content.
"""
    else:
        prompt = f"""
Generate a docker-compose.yml for production deployment.

Services to include:
{services_desc}

Stack: {state.get('detected_stack', 'unknown')}
Repo scan: {json.dumps(scan, indent=2)}

Rules:
- Each app service should build from its respective build context directory with the correct Dockerfile.
- Infer any external services needed (postgres, redis, etc.) from the codebase and add them.
- Use environment variables for credentials (with placeholder values).
- Use volumes for data persistence.
- Output ONLY YAML, no markdown wrappers.
- Do NOT include any explanations, analysis, or commentary. Return ONLY the raw YAML content.
"""

    resp = llm_compose.invoke(prompt)
    compose = strip_markdown_wrapper(resp.content, lang="yaml")
    state["docker_compose"] = compose
    
    return state
