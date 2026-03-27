from typing import Dict, Any
import json
import re
import yaml
from langchain_core.runnables.config import RunnableConfig
from .llm_config import llm_compose, strip_markdown_wrapper, RETRY_CONFIGS, FALLBACK_PROMPTS
from graph.llm_retry import invoke_with_retry
from tools.example_bank import fetch_reference_examples, format_examples_for_prompt


def _normalize_path(path: str) -> str:
    normalized = (path or ".").replace("\\", "/").strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized or "."


def _build_deterministic_compose(services: list[dict[str, Any]]) -> str:
    """Generate a deterministic baseline compose file from planner services."""
    compose: dict[str, Any] = {
        "services": {},
        "networks": {
            "app-network": {
                "driver": "bridge",
            }
        },
    }

    for svc in services:
        if not isinstance(svc, dict):
            continue
        name = str(svc.get("name", "")).strip()
        if not name:
            continue

        build_context = str(svc.get("build_context", ".") or ".")
        dockerfile_path = _normalize_dockerfile_path(str(svc.get("dockerfile_path", "") or ""))
        try:
            port_int = int(svc.get("port")) if svc.get("port") is not None else None
        except (TypeError, ValueError):
            port_int = None

        build: Any
        if dockerfile_path:
            build = {
                "context": build_context,
                "dockerfile": dockerfile_path,
            }
        else:
            build = build_context

        entry: dict[str, Any] = {
            "build": build,
            "restart": "unless-stopped",
            "networks": ["app-network"],
        }
        if port_int:
            entry["ports"] = [f"{port_int}:{port_int}"]

        compose["services"][name] = entry

    return yaml.safe_dump(compose, sort_keys=False)


def _normalize_dockerfile_path(path: str) -> str:
    normalized = (path or "").replace("\\", "/").strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _extract_build_context(entry: dict[str, Any]) -> str | None:
    build = entry.get("build")
    if isinstance(build, str) and build.strip():
        return _normalize_path(build)
    if isinstance(build, dict):
        context = build.get("context")
        if isinstance(context, str) and context.strip():
            return _normalize_path(context)
    return None


def _extract_build_dockerfile(entry: dict[str, Any]) -> str | None:
    build = entry.get("build")
    if isinstance(build, dict):
        dockerfile = build.get("dockerfile")
        if isinstance(dockerfile, str) and dockerfile.strip():
            return _normalize_dockerfile_path(dockerfile)
    return None


def _first_container_port(entry: dict[str, Any]) -> int | None:
    ports = entry.get("ports")
    if not isinstance(ports, list):
        return None
    for value in ports:
        if isinstance(value, int):
            return int(value)
        if isinstance(value, str):
            # Accept common compose forms such as "3000:3000" or "127.0.0.1:3000:3000".
            match = re.search(r"(\d+)\s*$", value)
            if match:
                try:
                    return int(match.group(1))
                except ValueError:
                    continue
    return None


def _is_root_pnpm_monorepo(scan: dict[str, Any], services: list[dict[str, Any]]) -> bool:
    key_files = scan.get("key_files", {}) if isinstance(scan, dict) else {}
    if not isinstance(key_files, dict):
        key_files = {}

    has_root_lock = any(name in key_files for name in ("pnpm-lock.yaml", "pnpm-workspace.yaml"))
    if not has_root_lock or len(services) < 2:
        return False

    subdir_service_count = 0
    for svc in services:
        if not isinstance(svc, dict):
            continue
        ctx = _normalize_path(str(svc.get("build_context", ".") or "."))
        if ctx != ".":
            subdir_service_count += 1
    return subdir_service_count >= 2


def _sanitize_public_backend_url(entry: dict[str, Any]) -> bool:
    """Ensure browser-facing NEXT_PUBLIC_BACKEND_URL does not use internal Docker DNS."""
    changed = False
    env = entry.get("environment")

    def _needs_rewrite(value: str) -> bool:
        v = (value or "").strip().lower()
        return (
            "http://backend" in v
            or "https://backend" in v
            or "http://localhost" in v
            or "https://localhost" in v
            or "backend:" in v
        )

    if isinstance(env, list):
        new_env: list[Any] = []
        for item in env:
            if not isinstance(item, str) or "=" not in item:
                new_env.append(item)
                continue
            key, value = item.split("=", 1)
            if key.strip() == "NEXT_PUBLIC_BACKEND_URL" and _needs_rewrite(value):
                new_env.append("NEXT_PUBLIC_BACKEND_URL=${NEXT_PUBLIC_BACKEND_URL}")
                changed = True
            else:
                new_env.append(item)
        if changed:
            entry["environment"] = new_env
    elif isinstance(env, dict):
        current = env.get("NEXT_PUBLIC_BACKEND_URL")
        if isinstance(current, str) and _needs_rewrite(current):
            env["NEXT_PUBLIC_BACKEND_URL"] = "${NEXT_PUBLIC_BACKEND_URL}"
            changed = True

    return changed


def _is_browser_service_name(name: str) -> bool:
    lowered = (name or "").strip().lower()
    browser_tokens = ("web", "frontend", "front-end", "ui", "client", "next")
    backend_tokens = ("backend", "api", "server", "worker")
    if any(token in lowered for token in backend_tokens):
        return False
    return any(token in lowered for token in browser_tokens)


def _strip_next_public_env(entry: dict[str, Any]) -> bool:
    """Remove browser-only NEXT_PUBLIC_* env vars from non-browser services."""
    env = entry.get("environment")
    changed = False

    if isinstance(env, list):
        filtered: list[Any] = []
        for item in env:
            if isinstance(item, str) and item.strip().startswith("NEXT_PUBLIC_"):
                changed = True
                continue
            filtered.append(item)
        if changed:
            entry["environment"] = filtered
    elif isinstance(env, dict):
        to_delete = [key for key in env.keys() if str(key).startswith("NEXT_PUBLIC_")]
        for key in to_delete:
            env.pop(key, None)
            changed = True

    return changed


def _infer_dependency_usage(scan: dict[str, Any]) -> dict[str, bool]:
    key_files = scan.get("key_files", {}) if isinstance(scan, dict) else {}
    if not isinstance(key_files, dict):
        key_files = {}

    blob = "\n".join(
        str(content).lower()
        for content in key_files.values()
        if isinstance(content, str)
    )

    postgres_markers = [
        "postgres",
        "postgresql",
        "pg_isready",
        "database_url",
        "postgres://",
        "postgresql://",
        "typeorm",
        "prisma",
    ]
    redis_markers = [
        "redis",
        "ioredis",
        "bullmq",
        "cache",
        "redis://",
    ]

    uses_postgres = any(marker in blob for marker in postgres_markers)
    uses_redis = any(marker in blob for marker in redis_markers)
    return {
        "postgres": uses_postgres,
        "redis": uses_redis,
    }


def _strip_dev_bind_mounts(entry: dict[str, Any]) -> bool:
    """Drop host bind mounts from app services for production-oriented compose output."""
    volumes = entry.get("volumes")
    if not isinstance(volumes, list):
        return False

    new_volumes: list[Any] = []
    changed = False

    for volume in volumes:
        if isinstance(volume, str):
            source = volume.split(":", 1)[0].strip()
            if source.startswith("./") or source.startswith("../"):
                changed = True
                continue
        if isinstance(volume, dict):
            vol_type = str(volume.get("type", "")).strip().lower()
            source = str(volume.get("source", "")).strip()
            if vol_type == "bind" or source.startswith("./") or source.startswith("../"):
                changed = True
                continue
        new_volumes.append(volume)

    if changed:
        entry["volumes"] = new_volumes
    return changed


def _repair_compose_output(content: str, services: list[dict[str, Any]], scan: dict[str, Any]) -> str:
    """Best-effort normalization to keep compose output deployable and score-friendly."""
    if not content or not content.strip():
        return content

    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError:
        return content

    if not isinstance(parsed, dict):
        return content

    services_block = parsed.get("services")
    if not isinstance(services_block, dict):
        services_block = {}
        parsed["services"] = services_block

    changed = False
    monorepo_root_build = _is_root_pnpm_monorepo(scan, services)
    dependency_usage = _infer_dependency_usage(scan)
    key_files = scan.get("key_files", {}) if isinstance(scan, dict) else {}
    if not isinstance(key_files, dict):
        key_files = {}
    expected_names = {
        str(svc.get("name", "")).strip()
        for svc in services
        if isinstance(svc, dict) and str(svc.get("name", "")).strip()
    }

    # Canonicalize aliases by matching build context to expected services.
    for svc in services:
        if not isinstance(svc, dict):
            continue
        expected_name = str(svc.get("name", "")).strip()
        expected_ctx = _normalize_path(str(svc.get("build_context", ".") or "."))
        expected_df = _normalize_dockerfile_path(str(svc.get("dockerfile_path", "") or ""))
        if not expected_name or expected_name in services_block:
            continue

        alias_name = None
        for candidate_name, candidate_cfg in services_block.items():
            if not isinstance(candidate_cfg, dict):
                continue
            if str(candidate_name).strip() == expected_name:
                continue
            candidate_ctx = _extract_build_context(candidate_cfg)
            candidate_df = _extract_build_dockerfile(candidate_cfg)
            dockerfile_compatible = (
                not expected_df
                or not candidate_df
                or candidate_df == expected_df
            )
            if candidate_ctx and candidate_ctx == expected_ctx and dockerfile_compatible:
                alias_name = str(candidate_name)
                break

        if alias_name:
            services_block[expected_name] = services_block.pop(alias_name)
            changed = True

    for svc in services:
        if not isinstance(svc, dict):
            continue

        name = str(svc.get("name", "")).strip()
        build_context = str(svc.get("build_context", ".") or ".").strip() or "."
        dockerfile_path = _normalize_dockerfile_path(str(svc.get("dockerfile_path", "") or ""))
        if monorepo_root_build and build_context != ".":
            if not dockerfile_path:
                dockerfile_path = f"{_normalize_path(build_context)}/Dockerfile"
            build_context = "."
        port = svc.get("port")

        try:
            port_int = int(port) if port is not None else None
        except (TypeError, ValueError):
            port_int = None

        if not name:
            continue

        entry = services_block.get(name)
        if not isinstance(entry, dict):
            entry = {}
            services_block[name] = entry
            changed = True

        if "build" not in entry:
            if dockerfile_path:
                entry["build"] = {
                    "context": build_context,
                    "dockerfile": dockerfile_path,
                }
            else:
                entry["build"] = build_context
            changed = True
        elif dockerfile_path:
            build = entry.get("build")
            if isinstance(build, str):
                entry["build"] = {
                    "context": _normalize_path(build),
                    "dockerfile": dockerfile_path,
                }
                changed = True
            elif isinstance(build, dict):
                if not isinstance(build.get("context"), str) or not build.get("context", "").strip():
                    build["context"] = build_context
                    changed = True
                existing_df = build.get("dockerfile")
                if not isinstance(existing_df, str) or not existing_df.strip():
                    build["dockerfile"] = dockerfile_path
                    changed = True

        if monorepo_root_build:
            build = entry.get("build")
            target_df = dockerfile_path or f"{_normalize_path(str(svc.get('build_context', '.') or '.'))}/Dockerfile"
            if isinstance(build, str):
                entry["build"] = {
                    "context": ".",
                    "dockerfile": target_df,
                }
                changed = True
            elif isinstance(build, dict):
                if build.get("context") != ".":
                    build["context"] = "."
                    changed = True
                if build.get("dockerfile") != target_df:
                    build["dockerfile"] = target_df
                    changed = True

        if port_int:
            desired_mapping = f"{port_int}:{port_int}"
            ports = entry.get("ports")
            if not isinstance(ports, list):
                entry["ports"] = [desired_mapping]
                changed = True
            elif len(ports) == 0:
                ports.append(desired_mapping)
                changed = True

        if _is_browser_service_name(name):
            if _sanitize_public_backend_url(entry):
                changed = True
        else:
            if _strip_next_public_env(entry):
                changed = True

        if _strip_dev_bind_mounts(entry):
            changed = True

    # Remove duplicate alias services when canonical services already exist.
    names_snapshot = list(services_block.keys())
    for service_name in names_snapshot:
        name = str(service_name).strip()
        if name in expected_names:
            continue

        entry = services_block.get(service_name)
        if not isinstance(entry, dict):
            continue

        entry_ctx = _extract_build_context(entry)
        entry_df = _extract_build_dockerfile(entry)
        entry_port = _first_container_port(entry)
        if not entry_ctx:
            continue

        for expected in services:
            if not isinstance(expected, dict):
                continue
            expected_name = str(expected.get("name", "")).strip()
            if not expected_name or expected_name not in services_block:
                continue
            expected_ctx = _normalize_path(str(expected.get("build_context", ".") or "."))
            expected_df = _normalize_dockerfile_path(str(expected.get("dockerfile_path", "") or ""))
            if expected_ctx != entry_ctx:
                continue
            if expected_df and entry_df and expected_df != entry_df:
                continue

            canonical_entry = services_block.get(expected_name)
            if not isinstance(canonical_entry, dict):
                continue
            canonical_port = _first_container_port(canonical_entry)
            canonical_df = _extract_build_dockerfile(canonical_entry)
            if expected_df and canonical_df and canonical_df != expected_df:
                continue

            try:
                expected_port = int(expected.get("port")) if expected.get("port") is not None else None
            except (TypeError, ValueError):
                expected_port = None

            if entry_port is not None and canonical_port is not None and entry_port == canonical_port:
                services_block.pop(service_name, None)
                changed = True
                break
            if expected_port is not None and entry_port == expected_port and canonical_port == expected_port:
                services_block.pop(service_name, None)
                changed = True
                break

    # Drop dependency services that are not evidenced by the scanned repository.
    for dep_name in ("postgres", "redis"):
        dep_entry = services_block.get(dep_name)
        if not isinstance(dep_entry, dict):
            continue
        if dependency_usage.get(dep_name):
            continue
        services_block.pop(dep_name, None)
        changed = True

    # If dependency services are removed, remove orphaned named volumes.
    if isinstance(parsed.get("volumes"), dict):
        volume_map = parsed.get("volumes")
        used_named_volumes: set[str] = set()
        for entry in services_block.values():
            if not isinstance(entry, dict):
                continue
            volumes = entry.get("volumes")
            if not isinstance(volumes, list):
                continue
            for value in volumes:
                if isinstance(value, str):
                    source = value.split(":", 1)[0].strip()
                    if source and not source.startswith("./") and not source.startswith("../") and "/" not in source:
                        used_named_volumes.add(source)
                elif isinstance(value, dict):
                    if str(value.get("type", "")).strip().lower() == "volume":
                        source = str(value.get("source", "")).strip()
                        if source:
                            used_named_volumes.add(source)

        for volume_name in list(volume_map.keys()):
            if volume_name not in used_named_volumes:
                volume_map.pop(volume_name, None)
                changed = True

    # Add explicit network configuration for clearer service isolation.
    default_network_name = "app-network"
    networks_block = parsed.get("networks")
    if not isinstance(networks_block, dict) or not networks_block:
        parsed["networks"] = {default_network_name: {"driver": "bridge"}}
        networks_block = parsed["networks"]
        changed = True

    for entry in services_block.values():
        if not isinstance(entry, dict):
            continue
        svc_networks = entry.get("networks")
        if not isinstance(svc_networks, list) or not svc_networks:
            entry["networks"] = [default_network_name]
            changed = True

    if not changed:
        return content

    return yaml.safe_dump(parsed, sort_keys=False)


def compose_generator_node(state: Dict[str, Any], config: RunnableConfig = None) -> Dict[str, Any]:
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
        (
            f"  - {s['name']}: build context={s['build_context']}, "
            f"dockerfile={s.get('dockerfile_path') or 'Dockerfile'}, port={s['port']}"
        )
        for s in services
    ])

    examples = fetch_reference_examples(
        artifact_type="compose",
        detected_stack=state.get("detected_stack", "unknown"),
        stack_tokens=state.get("stack_tokens", []),
        service=None,
        limit=3,
    )
    references = format_examples_for_prompt(examples)
    
    baseline_compose = existing_compose if isinstance(existing_compose, str) and existing_compose.strip() else _build_deterministic_compose(services)
    baseline_compose = _repair_compose_output(baseline_compose, services, scan)

    if existing_compose:
        prompt = f"""
You are a DevOps expert refining a deterministic baseline docker-compose.yml.

Services in this repo:
{services_desc}

Stack: {state.get('detected_stack', 'unknown')}

DETERMINISTIC BASELINE docker-compose.yml:
{baseline_compose}

EXISTING docker-compose.yml:
{existing_compose}

REFERENCE EXAMPLES (adapt style/patterns, do not copy verbatim):
{references}

Improve the deterministic baseline while preserving correctness. If no improvements are needed, return the baseline as-is.

Rules:
- Each app service should build from its respective directory with the correct Dockerfile.
- For pnpm monorepos with a root lockfile, prefer `build.context: .` and service Dockerfiles like `apps/backend/Dockerfile`.
- If two services use the same Dockerfile filename (for example both are named Dockerfile), keep them as separate services when build contexts differ.
- Every declared app service must define a ports mapping list (for example: "3000:3000").
- Avoid dev-only bind mounts for app code in production compose output.
- `NEXT_PUBLIC_BACKEND_URL` must be externally configurable (for example `${{NEXT_PUBLIC_BACKEND_URL}}`), not internal Docker DNS like `http://backend:5000`.
- Add external services (postgres, redis, etc.) if the codebase references them but they're missing.
- Use environment variables for credentials (with placeholder values).
- Output ONLY YAML, no markdown wrappers.
- Do NOT include any explanations, analysis, or commentary. Return ONLY the raw YAML content.
- Reuse useful patterns from REFERENCE EXAMPLES where applicable, but do not copy exact text.
"""
    else:
        prompt = f"""
    You are a DevOps expert refining a deterministic baseline docker-compose.yml.

Services to include:
{services_desc}

Stack: {state.get('detected_stack', 'unknown')}
Repo scan: {json.dumps(scan, indent=2)}

DETERMINISTIC BASELINE docker-compose.yml:
{baseline_compose}

REFERENCE EXAMPLES (adapt style/patterns, do not copy verbatim):
{references}

Improve the baseline compose file using these rules:
- Each app service should build from its respective build context directory with the correct Dockerfile.
- For pnpm monorepos with a root lockfile, prefer `build.context: .` and service Dockerfiles like `apps/backend/Dockerfile`.
- If two services use the same Dockerfile filename (for example both are named Dockerfile), keep them as separate services when build contexts differ.
- Every declared app service must define a ports mapping list (for example: "3000:3000").
- Avoid dev-only bind mounts for app code in production compose output.
- `NEXT_PUBLIC_BACKEND_URL` must be externally configurable (for example `${{NEXT_PUBLIC_BACKEND_URL}}`), not internal Docker DNS like `http://backend:5000`.
- Infer any external services needed (postgres, redis, etc.) from the codebase and add them.
- Use environment variables for credentials (with placeholder values).
- Use volumes for data persistence.
- Output ONLY YAML, no markdown wrappers.
- Do NOT include any explanations, analysis, or commentary. Return ONLY the raw YAML content.
- Reuse useful patterns from REFERENCE EXAMPLES where applicable, but do not copy exact text.
"""

    try:
        response, attempts_used, fallback_used = invoke_with_retry(
            invoke_fn=lambda raw_prompt: llm_compose.invoke(raw_prompt, config=config),
            prompt=prompt,
            fallback_prompt=FALLBACK_PROMPTS["compose"],
            config=RETRY_CONFIGS["compose"],
            node_name="compose_gen",
        )
        compose = strip_markdown_wrapper(response.content, lang="yaml")
        compose = _repair_compose_output(compose, services, scan)
        state["docker_compose"] = compose
        state["compose_retry_attempts"] = attempts_used
        state["compose_fallback_used"] = fallback_used
    except Exception as e:
        state["docker_compose"] = baseline_compose
        state["compose_retry_attempts"] = RETRY_CONFIGS["compose"].max_attempts
        state["compose_fallback_used"] = True
        state["compose_generation_warning"] = f"llm_refine_failed:{e}"
    
    return state
