from typing import Dict, Any
import json
import re
from .llm_config import llm_docker, strip_markdown_wrapper, RETRY_CONFIGS, FALLBACK_PROMPTS
from graph.llm_retry import invoke_with_retry
from tools.example_bank import fetch_reference_examples, format_examples_for_prompt


def _extract_package_scripts(key_files: dict[str, Any], build_ctx: str) -> list[str]:
    if not isinstance(key_files, dict):
        return []
    normalized_ctx = (build_ctx or ".").replace("\\", "/").strip()
    if normalized_ctx.startswith("./"):
        normalized_ctx = normalized_ctx[2:]
    package_path = "package.json" if normalized_ctx in ("", ".") else f"{normalized_ctx}/package.json"
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


def _normalize_ctx(path: str) -> str:
    normalized = (path or ".").replace("\\", "/").strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized or "."


def _repair_dockerfile_output(
    content: str,
    service: dict[str, Any],
    key_files: dict[str, Any],
    available_scripts: list[str],
) -> str:
    """Apply deterministic fixes for common monorepo/docker pitfalls in generated Dockerfiles."""
    if not content or not content.strip():
        return content

    fixed = content
    changed = False

    build_ctx = _normalize_ctx(str(service.get("build_context", ".") or "."))
    has_root_pnpm_lock = isinstance(key_files, dict) and "pnpm-lock.yaml" in key_files

    # In monorepos with root lockfile, ensure service Dockerfiles copy root lockfile,
    # not a non-existent nested lockfile like apps/web/pnpm-lock.yaml.
    if has_root_pnpm_lock and build_ctx != ".":
        wrong_lock_patterns = [
            rf"{re.escape(build_ctx)}/pnpm-lock\.yaml\*?",
            rf"{re.escape(build_ctx)}/pnpm-lock\.yaml",
        ]
        for pattern in wrong_lock_patterns:
            new_fixed = re.sub(pattern, "pnpm-lock.yaml*", fixed)
            if new_fixed != fixed:
                fixed = new_fixed
                changed = True

    scripts_set = {s.strip() for s in available_scripts}

    # Never swallow build failures with `|| true` in production Dockerfiles.
    new_fixed = re.sub(r"(?im)^\s*RUN\s+pnpm\s+build\s*\|\|\s*true\s*$", "RUN pnpm build", fixed)
    if new_fixed != fixed:
        fixed = new_fixed
        changed = True

    # If no build script exists, drop pnpm build line to avoid missing-script failures.
    if "build" not in scripts_set:
        new_fixed = re.sub(r"(?im)^\s*RUN\s+pnpm\s+build\s*$\n?", "", fixed)
        if new_fixed != fixed:
            fixed = new_fixed
            changed = True

    # Prefer root path healthchecks for generated artifacts.
    new_fixed = re.sub(r"(http://localhost:\d+)/health\b", r"\1/", fixed)
    if new_fixed != fixed:
        fixed = new_fixed
        changed = True

    return fixed if changed else content


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
        available_scripts = _extract_package_scripts(key_files, build_ctx)
        scripts_hint = (
            f"Detected package.json scripts for this service: {', '.join(available_scripts)}"
            if available_scripts
            else "Detected package.json scripts for this service: none/unknown"
        )
        
        # Look up the pre-existing Dockerfile using the planner-provided path
        existing_dockerfile = None
        if dockerfile_path:
            existing_dockerfile = key_files.get(dockerfile_path)
        
        if existing_dockerfile:
            examples = fetch_reference_examples(
                artifact_type="dockerfile",
                detected_stack=state.get("detected_stack", "unknown"),
                stack_tokens=state.get("stack_tokens", []),
                service=service,
                limit=3,
            )
            references = format_examples_for_prompt(examples)

            prompt = f"""
You are a DevOps expert reviewing an existing Dockerfile.

Service: {svc_name}
Build context: {build_ctx}
Port: {port}
Stack: {state.get('detected_stack', 'unknown')}
{scripts_hint}

EXISTING Dockerfile:
{existing_dockerfile}

REFERENCE EXAMPLES (adapt style/patterns, do not copy verbatim):
{references}

Review this Dockerfile. If it follows production best practices (multi-stage builds, non-root user, slim images, proper EXPOSE/HEALTHCHECK), return it AS-IS.
If it can be improved, return the IMPROVED version.

Rules:
1. Use multi-stage builds if not already present.
2. Use slim/alpine base images.
3. Do NOT copy node_modules / venv directly, build inside builder stage.
4. Run as non-root user.
5. EXPOSE the correct port and add HEALTHCHECK.
6. Use `http://localhost:<port>/` for HTTP healthchecks (avoid `/health` unless strongly required by code evidence).
7. Only run `pnpm build` when a `build` script exists for this service.
8. If no `build` script exists, do not run build; use a runtime command that matches available scripts/artifacts.
9. Output ONLY Dockerfile content, no explanations. Do not wrap in markdown.
10. Do NOT include any preamble like 'IMPROVED Dockerfile:' or commentary. Return ONLY the raw Dockerfile.
11. Reuse useful patterns from REFERENCE EXAMPLES where applicable, but do not copy exact text.
"""
        else:
            examples = fetch_reference_examples(
                artifact_type="dockerfile",
                detected_stack=state.get("detected_stack", "unknown"),
                stack_tokens=state.get("stack_tokens", []),
                service=service,
                limit=3,
            )
            references = format_examples_for_prompt(examples)

            prompt = f"""
Generate a PRODUCTION Dockerfile.

Service: {svc_name}
Build context: {build_ctx}
Port: {port}
Stack: {state.get('detected_stack', 'unknown')}
{scripts_hint}
Repo scan: {json.dumps(scan, indent=2)}

REFERENCE EXAMPLES (adapt style/patterns, do not copy verbatim):
{references}

Rules:
1. Use multi-stage builds.
2. Use slim/alpine base images.
3. Do NOT copy node_modules / venv directly from host, build inside the builder stage.
4. Run as non-root user.
5. EXPOSE the port and add HEALTHCHECK.
6. Use `http://localhost:<port>/` for HTTP healthchecks (avoid `/health` unless strongly required by code evidence).
7. Only run `pnpm build` when a `build` script exists for this service.
8. If no `build` script exists, do not run build; use a runtime command that matches available scripts/artifacts.
9. Output ONLY Dockerfile content, no explanations. Do not wrap in markdown.
10. Do NOT include any preamble or commentary. Return ONLY the raw Dockerfile.
11. Reuse useful patterns from REFERENCE EXAMPLES where applicable, but do not copy exact text.
"""
        
        try:
            response, _, _ = invoke_with_retry(
                invoke_fn=lambda raw_prompt: llm_docker.invoke(raw_prompt),
                prompt=prompt,
                fallback_prompt=FALLBACK_PROMPTS["docker"],
                config=RETRY_CONFIGS["docker"],
                node_name=f"docker_gen:{svc_name}",
            )
            dockerfile = strip_markdown_wrapper(response.content)
            dockerfile = _repair_dockerfile_output(
                dockerfile,
                service=service,
                key_files=key_files,
                available_scripts=available_scripts,
            )
            dockerfiles[svc_name] = dockerfile
        except Exception as e:
            state["error"] = f"Failed generating Dockerfile for {svc_name}: {e}"
            return state
    
    state["dockerfiles"] = dockerfiles
    return state
