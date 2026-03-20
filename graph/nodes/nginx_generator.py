from typing import Dict, Any
import re
from .llm_config import llm_nginx, strip_markdown_wrapper, RETRY_CONFIGS, FALLBACK_PROMPTS
from graph.llm_retry import invoke_with_retry


def _count_braces(content: str) -> tuple[int, int]:
    opens = content.count("{")
    closes = content.count("}")
    return opens, closes


def _is_balanced(content: str) -> bool:
    depth = 0
    for ch in content:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _indent(text: str, prefix: str = "  ") -> str:
    lines = text.splitlines()
    return "\n".join(f"{prefix}{line}" if line.strip() else line for line in lines)


def _infer_frontend_port(services: list[dict[str, Any]]) -> int:
    for svc in services:
        if not isinstance(svc, dict):
            continue
        name = str(svc.get("name", "")).strip().lower()
        if any(token in name for token in ("web", "frontend", "ui", "client", "next")):
            try:
                return int(svc.get("port", 3000))
            except (TypeError, ValueError):
                return 3000

    for svc in services:
        if not isinstance(svc, dict):
            continue
        try:
            port = int(svc.get("port"))
        except (TypeError, ValueError):
            continue
        if port == 3000:
            return 3000

    if services and isinstance(services[0], dict):
        try:
            return int(services[0].get("port", 3000))
        except (TypeError, ValueError):
            return 3000
    return 3000


def _infer_backend_port(services: list[dict[str, Any]]) -> int:
    for svc in services:
        if not isinstance(svc, dict):
            continue
        name = str(svc.get("name", "")).strip().lower()
        if any(token in name for token in ("backend", "api", "server")):
            try:
                return int(svc.get("port", 5000))
            except (TypeError, ValueError):
                return 5000

    for svc in services:
        if not isinstance(svc, dict):
            continue
        try:
            port = int(svc.get("port"))
        except (TypeError, ValueError):
            continue
        if port == 5000:
            return 5000

    if len(services) > 1 and isinstance(services[1], dict):
        try:
            return int(services[1].get("port", 5000))
        except (TypeError, ValueError):
            return 5000
    return 5000


def _default_nginx_conf(services: list[dict[str, Any]]) -> str:
    frontend_port = _infer_frontend_port(services)
    default_csp = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self' data: https:; connect-src 'self' https: ws: wss:;"

    return (
        "events { worker_connections 1024; }\n\n"
        "http {\n"
        "  server {\n"
        "    listen 80;\n"
        "    server_name _;\n\n"
        "    add_header X-Content-Type-Options \"nosniff\" always;\n"
        "    add_header X-Frame-Options \"SAMEORIGIN\" always;\n"
        f"    add_header Content-Security-Policy \"{default_csp}\" always;\n\n"
        "    location / {\n"
        f"      proxy_pass http://localhost:{frontend_port};\n"
        "      proxy_http_version 1.1;\n"
        "      proxy_set_header Host $host;\n"
        "      proxy_set_header X-Real-IP $remote_addr;\n"
        "      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "      proxy_set_header X-Forwarded-Proto $scheme;\n"
        "      proxy_set_header Upgrade $http_upgrade;\n"
        "      proxy_set_header Connection \"upgrade\";\n"
        "    }\n"
        "  }\n"
        "}\n"
    )


def _repair_nginx_output(content: str, services: list[dict[str, Any]]) -> str:
    """Best-effort normalization to enforce basic nginx config structure."""
    if not content or not content.strip():
        return _default_nginx_conf(services)

    repaired = content.strip()
    lower = repaired.lower()

    has_server_block = "server{" in lower or "server {" in lower
    has_http_block = "http{" in lower or "http {" in lower
    has_events_block = "events{" in lower or "events {" in lower

    if not has_server_block:
        return _default_nginx_conf(services)

    if not has_http_block:
        repaired = f"http {{\n{_indent(repaired)}\n}}"

    lower = repaired.lower()
    has_http_block = "http{" in lower or "http {" in lower
    has_events_block = "events{" in lower or "events {" in lower
    if not has_events_block and not has_http_block:
        repaired = f"events {{ worker_connections 1024; }}\n\n{repaired}"

    if not _is_balanced(repaired):
        opens, closes = _count_braces(repaired)
        if opens > closes:
            repaired = repaired + ("\n" + "\n".join("}" for _ in range(opens - closes)))
        else:
            return _default_nginx_conf(services)

    if not _is_balanced(repaired):
        return _default_nginx_conf(services)

    # Smart-deploy currently uses host nginx, so upstreams should target localhost published ports.
    frontend_port = _infer_frontend_port(services)
    backend_port = _infer_backend_port(services)
    repaired = re.sub(
        rf"(?im)(proxy_pass\s+http://)(web|frontend|ui|client|next):{frontend_port}(\s*;)",
        rf"\1localhost:{frontend_port}\3",
        repaired,
    )
    repaired = re.sub(
        rf"(?im)(proxy_pass\s+http://)(backend|api|server):{backend_port}(\s*;)",
        rf"\1localhost:{backend_port}\3",
        repaired,
    )

    # Keep whatever CSP the model provides as long as the config structure is valid.

    return repaired.strip() + "\n"


def _infer_route_guidance(scan: Dict[str, Any], services: list[dict[str, Any]]) -> str:
    """Infer whether /api and /ws routes are likely required based on scan evidence."""
    key_files = scan.get("key_files", {})
    if not isinstance(key_files, dict):
        key_files = {}

    service_names = [str(s.get("name", "")).lower() for s in services if isinstance(s, dict)]
    has_backend_service = any(
        token in name
        for name in service_names
        for token in ("api", "backend", "server")
    )

    ws_markers = ["socket.io", "websocket", "/ws", "ws://", "wss://", "proxy_set_header upgrade"]
    api_markers = [
        "app.use('/api",
        'app.use("/api',
        "router.prefix('/api",
        'router.prefix("/api',
        "@requestmapping(\"/api",
        "@requestmapping('/api",
        "path=\"/api",
        "path='/api",
        "location /api",
        "next_public_api",
    ]

    all_content = "\n".join(
        str(content).lower()
        for content in key_files.values()
        if isinstance(content, str)
    )

    has_ws_evidence = any(marker in all_content for marker in ws_markers) or any(
        token in name for name in service_names for token in ("ws", "socket")
    )
    has_api_prefix_evidence = any(marker in all_content for marker in api_markers)

    include_api = has_api_prefix_evidence
    include_ws = has_ws_evidence

    return (
        "ROUTE DECISION GUIDANCE (derived from repo evidence):\n"
        f"- Backend service detected: {'yes' if has_backend_service else 'no'}\n"
        f"- Include `/api` location: {'yes' if include_api else 'no'}\n"
        f"- Include `/ws` location: {'yes' if include_ws else 'no'}\n"
        "- Do not add a `/api` location block unless evidence indicates an `/api` prefix.\n"
        "- If backend exists but `/api` prefix is not indicated, route only explicit websocket paths and keep frontend at `/`.\n"
    )


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
    route_guidance = _infer_route_guidance(scan, services)
    
    baseline_nginx = existing_nginx if isinstance(existing_nginx, str) and existing_nginx.strip() else _default_nginx_conf(services)
    baseline_nginx = _repair_nginx_output(baseline_nginx, services)

    if existing_nginx:
        prompt = f"""
You are a DevOps expert refining a deterministic baseline nginx.conf.

Services:
{services_desc}

{route_guidance}

DETERMINISTIC BASELINE nginx.conf:
{baseline_nginx}

EXISTING nginx.conf:
{existing_nginx}

Improve the deterministic baseline while preserving correctness. If no improvements are needed, return the baseline as-is.

Rules:
- Listen on port 80.
- Always include structurally complete config blocks with balanced braces.
- Route traffic to each service appropriately.
- Add `/api` only when repo evidence indicates backend routes use an `/api` prefix.
- Add `/ws` only when websocket behavior is indicated.
- Assume nginx runs on the host OS (outside compose), so use localhost upstreams (for example `proxy_pass http://localhost:3000`).
- Include ALL of these security headers: X-Frame-Options, X-Content-Type-Options, Content-Security-Policy.
- Keep Content-Security-Policy practical for modern frontends (allow `unsafe-inline` for scripts/styles when needed).
- Include proper proxy headers (X-Real-IP, X-Forwarded-For).
- For WebSocket services, include proper upgrade headers.
- Output ONLY nginx config, no markdown wrappers.
"""
    else:
        prompt = f"""
    You are a DevOps expert refining a deterministic baseline nginx.conf.

Services:
{services_desc}

{route_guidance}

DETERMINISTIC BASELINE nginx.conf:
{baseline_nginx}

Improve the baseline config using these rules:
- Listen on port 80.
- Always include structurally complete config blocks with balanced braces.
- Route traffic to each service appropriately.
- Add `/api` only when repo evidence indicates backend routes use an `/api` prefix.
- Add `/ws` only when websocket behavior is indicated.
- Assume nginx runs on the host OS (outside compose), so use localhost upstreams (for example `proxy_pass http://localhost:3000`).
- Include ALL of these security headers: X-Frame-Options, X-Content-Type-Options, Content-Security-Policy.
- Keep Content-Security-Policy practical for modern frontends (allow `unsafe-inline` for scripts/styles when needed).
- Include proper proxy headers (X-Real-IP, X-Forwarded-For).
- For WebSocket services, include proper upgrade headers (Connection, Upgrade).
- Output ONLY nginx config, no markdown wrappers.
"""

    try:
        response, attempts_used, fallback_used = invoke_with_retry(
            invoke_fn=lambda raw_prompt: llm_nginx.invoke(raw_prompt),
            prompt=prompt,
            fallback_prompt=FALLBACK_PROMPTS["nginx"],
            config=RETRY_CONFIGS["nginx"],
            node_name="nginx_gen",
        )
        nginx_conf = strip_markdown_wrapper(response.content, lang="nginx")
        if nginx_conf.startswith("conf\n"):
            nginx_conf = nginx_conf[5:]
        nginx_conf = _repair_nginx_output(nginx_conf, services)
        state["nginx_conf"] = nginx_conf
        state["nginx_retry_attempts"] = attempts_used
        state["nginx_fallback_used"] = fallback_used
    except Exception as e:
        state["nginx_conf"] = baseline_nginx
        state["nginx_retry_attempts"] = RETRY_CONFIGS["nginx"].max_attempts
        state["nginx_fallback_used"] = True
        state["nginx_generation_warning"] = f"llm_refine_failed:{e}"
    
    return state
