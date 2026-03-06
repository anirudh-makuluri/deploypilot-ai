from typing import Dict, Any
from .llm_config import llm_nginx, strip_markdown_wrapper


def nginx_generator_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Generate an nginx.conf for production deployment with multi-service routing."""
    scan = state.get("repo_scan", {})
    key_files = scan.get("key_files", {})
    services = state.get("services", [])
    
    # Check for existing nginx config
    existing_nginx = None
    for path, content in key_files.items():
        filename = path.split("/")[-1]
        if filename == "nginx.conf":
            existing_nginx = content
            break
    
    services_desc = "\n".join([
        f"  - {s['name']}: port={s['port']}"
        for s in services
    ])
    
    if existing_nginx:
        prompt = f"""
You are a DevOps expert reviewing an existing nginx.conf.

Services:
{services_desc}

EXISTING nginx.conf:
{existing_nginx}

Review this nginx config. If it correctly routes to all services with proper security headers, return it AS-IS.
If it can be improved, return the IMPROVED version.

Rules:
- Listen on port 80.
- Route traffic to each service appropriately (e.g., frontend on /, WebSocket on /ws, API on /api).
- Include security headers (X-Frame-Options, X-Content-Type-Options, HSTS, etc.).
- Include proper proxy headers (X-Real-IP, X-Forwarded-For).
- For WebSocket services, include proper upgrade headers.
- Output ONLY nginx config, no markdown wrappers.
"""
    else:
        prompt = f"""
You are a DevOps expert given the task to write a nginx.conf for production deployment.

Services:
{services_desc}

Rules:
- Listen on port 80.
- Route traffic to each service appropriately (e.g., frontend on /, WebSocket on /ws, API on /api).
- Use docker-compose service names as upstream hostnames (e.g., proxy_pass http://frontend:3000).
- Include security headers (X-Frame-Options, X-Content-Type-Options, HSTS, etc.).
- Include proper proxy headers (X-Real-IP, X-Forwarded-For).
- For WebSocket services, include proper upgrade headers (Connection, Upgrade).
- Output ONLY nginx config, no markdown wrappers.
"""

    resp = llm_nginx.invoke(prompt)
    nginx_conf = strip_markdown_wrapper(resp.content, lang="nginx")
    if nginx_conf.startswith("conf\n"):
        nginx_conf = nginx_conf[5:]
    state["nginx_conf"] = nginx_conf
    
    return state
