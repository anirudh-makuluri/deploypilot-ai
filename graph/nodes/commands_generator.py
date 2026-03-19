from __future__ import annotations

from typing import Any, Dict
import json


def _normalize_ctx(path: str) -> str:
    normalized = (path or ".").replace("\\", "/").strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized or "."


def _extract_package_scripts(key_files: dict[str, Any], build_ctx: str) -> list[str]:
    if not isinstance(key_files, dict):
        return []
    normalized_ctx = _normalize_ctx(build_ctx)
    package_path = "package.json" if normalized_ctx == "." else f"{normalized_ctx}/package.json"
    content = key_files.get(package_path)
    if not isinstance(content, str) or '"scripts"' not in content:
        return []
    try:
        package_json = json.loads(content)
    except Exception:
        return []
    scripts = package_json.get("scripts", {})
    if not isinstance(scripts, dict):
        return []
    return sorted(str(name) for name in scripts.keys())


def _install_command(tokens: set[str], scripts: set[str]) -> str:
    if "python" in tokens:
        if "requirements.txt" in scripts:
            return "pip install -r requirements.txt"
        return "pip install -r requirements.txt"
    if "pnpm" in tokens:
        return "pnpm install --frozen-lockfile"
    if "yarn" in tokens:
        return "yarn install --frozen-lockfile"
    if any(tok in tokens for tok in ("node", "next", "react", "vite", "vue", "angular", "svelte")):
        return "npm ci"
    return "# install command not confidently inferred"


def _build_command(tokens: set[str], scripts: set[str]) -> str:
    if "build" in scripts:
        if "pnpm" in tokens:
            return "pnpm build"
        if "yarn" in tokens:
            return "yarn build"
        return "npm run build"
    if "python" in tokens:
        return "python -m compileall ."
    return "# no explicit build command inferred"


def _run_command(tokens: set[str], scripts: set[str], port: int | None) -> str:
    if "start" in scripts:
        if "pnpm" in tokens:
            return "pnpm start"
        if "yarn" in tokens:
            return "yarn start"
        return "npm start"
    if "dev" in scripts:
        if "pnpm" in tokens:
            return "pnpm dev"
        if "yarn" in tokens:
            return "yarn dev"
        return "npm run dev"
    if "python" in tokens:
        inferred_port = port or 8000
        return f"python -m uvicorn main:app --host 0.0.0.0 --port {inferred_port}"
    return "# run command not confidently inferred"


def commands_generator_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Build install/build/run command suggestions for each app service."""
    services = state.get("services", [])
    stack_tokens = [str(t).lower() for t in state.get("stack_tokens", []) if isinstance(t, str)]
    token_set = set(stack_tokens)
    scan = state.get("repo_scan", {}) if isinstance(state.get("repo_scan", {}), dict) else {}
    key_files = scan.get("key_files", {}) if isinstance(scan, dict) else {}

    by_service: Dict[str, Dict[str, str]] = {}

    for service in services:
        if not isinstance(service, dict):
            continue
        name = str(service.get("name", "")).strip()
        if not name:
            continue

        build_ctx = str(service.get("build_context", ".") or ".")
        normalized_ctx = _normalize_ctx(build_ctx)
        scripts = set(_extract_package_scripts(key_files, normalized_ctx))

        lock_path = "pnpm-lock.yaml" if normalized_ctx == "." else f"{normalized_ctx}/pnpm-lock.yaml"
        yarn_lock_path = "yarn.lock" if normalized_ctx == "." else f"{normalized_ctx}/yarn.lock"

        service_tokens = set(token_set)
        if isinstance(key_files, dict):
            if lock_path in key_files:
                service_tokens.add("pnpm")
            if yarn_lock_path in key_files:
                service_tokens.add("yarn")

        try:
            port = int(service.get("port")) if service.get("port") is not None else None
        except (TypeError, ValueError):
            port = None

        by_service[name] = {
            "install": _install_command(service_tokens, scripts),
            "build": _build_command(service_tokens, scripts),
            "run": _run_command(service_tokens, scripts, port),
        }

    global_commands = []
    if isinstance(services, list) and len(services) > 1:
        global_commands.extend([
            "docker compose build",
            "docker compose up -d",
            "docker compose logs -f",
        ])
    else:
        single = None
        if isinstance(services, list) and services and isinstance(services[0], dict):
            single = services[0]
        if single:
            svc_name = str(single.get("name", "app") or "app")
            try:
                svc_port = int(single.get("port"))
            except (TypeError, ValueError):
                svc_port = 8000
            global_commands.extend([
                f"docker build -t {svc_name}:latest .",
                f"docker run --rm -p {svc_port}:{svc_port} {svc_name}:latest",
            ])

    state["commands"] = {
        "by_service": by_service,
        "global": global_commands,
    }
    return state
