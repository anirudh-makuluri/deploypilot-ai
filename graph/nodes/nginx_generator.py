from typing import Dict, Any
import re
from langchain_core.runnables.config import RunnableConfig


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


def _extract_location_block(content: str, location_start: int) -> tuple[int, int] | None:
    """Return [start, end) byte offsets for a location block starting at location_start."""
    open_brace = content.find("{", location_start)
    if open_brace == -1:
        return None

    depth = 0
    for idx in range(open_brace, len(content)):
        ch = content[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return location_start, idx + 1
    return None


def _remove_ws_location_blocks(content: str) -> str:
    """Remove explicit /ws nginx location blocks when websocket routing is not required."""
    pattern = re.compile(r"(?im)^\s*location\s+(?:=\s*)?/ws(?:/|\b)")
    search_from = 0
    result = content

    while True:
        match = pattern.search(result, search_from)
        if not match:
            break
        block_span = _extract_location_block(result, match.start())
        if not block_span:
            search_from = match.end()
            continue
        block_start, block_end = block_span
        result = result[:block_start] + result[block_end:]
        search_from = block_start

    return result


def _infer_route_flags(scan: Dict[str, Any], services: list[dict[str, Any]]) -> tuple[bool, bool, bool]:
    """Infer backend presence and whether /api or /ws routes should be generated."""
    key_files = scan.get("key_files", {})
    if not isinstance(key_files, dict):
        key_files = {}

    service_names = [str(s.get("name", "")).lower() for s in services if isinstance(s, dict)]
    service_tokens = {
        token
        for name in service_names
        for token in re.split(r"[^a-z0-9]+", name)
        if token
    }
    has_backend_service = any(token in service_tokens for token in ("api", "backend", "server"))

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

    ws_patterns = [
        r"\bsocket\.io\b",
        r"\bwebsocket(s)?\b",
        r"\bws://",
        r"\bwss://",
        r"location\s+(?:=\s*)?/ws(?:/|\b)",
        r"[\"']/(ws|socket)(?:/|[\"'])",
    ]

    has_ws_evidence = any(re.search(pattern, all_content, re.IGNORECASE) for pattern in ws_patterns) or any(
        token in service_tokens for token in ("ws", "socket", "websocket")
    )
    has_api_prefix_evidence = any(marker in all_content for marker in api_markers)

    return has_backend_service, has_api_prefix_evidence, has_ws_evidence


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
            port = int(svc.get("port", 3000))
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


def _infer_frontend_service_name(services: list[dict[str, Any]]) -> str:
    for svc in services:
        if not isinstance(svc, dict):
            continue
        name = str(svc.get("name", "")).strip()
        lowered = name.lower()
        if any(token in lowered for token in ("web", "frontend", "ui", "client", "next", "dashboard")):
            return name or "web"
    if services and isinstance(services[0], dict):
        return str(services[0].get("name", "")).strip() or "web"
    return "web"


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
            port = int(svc.get("port", 5000))
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
    frontend_service = _infer_frontend_service_name(services)
    default_csp = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https:; font-src 'self' data: https:; connect-src 'self' https: wss:;"

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
        f"      proxy_pass http://{frontend_service}:{frontend_port};\n"
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


def _repair_nginx_output(content: str, services: list[dict[str, Any]], include_ws: bool = True) -> str:
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

    # Preserve service DNS upstreams when present (compose networking).

    if not include_ws:
        repaired = _remove_ws_location_blocks(repaired)

    # Keep whatever CSP the model provides as long as the config structure is valid.

    return repaired.strip() + "\n"


def _infer_route_guidance(scan: Dict[str, Any], services: list[dict[str, Any]]) -> str:
    """Infer whether /api and /ws routes are likely required based on scan evidence."""
    has_backend_service, include_api, include_ws = _infer_route_flags(scan, services)

    return (
        "ROUTE DECISION GUIDANCE (derived from repo evidence):\n"
        f"- Backend service detected: {'yes' if has_backend_service else 'no'}\n"
        f"- Include `/api` location: {'yes' if include_api else 'no'}\n"
        f"- Include `/ws` location: {'yes' if include_ws else 'no'}\n"
        "- Do not add a `/api` location block unless evidence indicates an `/api` prefix.\n"
        "- If backend exists but `/api` prefix is not indicated, route only explicit websocket paths and keep frontend at `/`.\n"
    )


def nginx_generator_node(state: Dict[str, Any], config: RunnableConfig) -> Dict[str, Any]:
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
    
    _, _, include_ws = _infer_route_flags(scan, services)
    
    baseline_nginx = existing_nginx if isinstance(existing_nginx, str) and existing_nginx.strip() else _default_nginx_conf(services)
    baseline_nginx = _repair_nginx_output(baseline_nginx, services, include_ws=include_ws)
    state["nginx_conf"] = baseline_nginx
    return state
