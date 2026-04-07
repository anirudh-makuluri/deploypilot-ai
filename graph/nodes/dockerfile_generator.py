from typing import Dict, Any
import json
import re
from langchain_core.runnables.config import RunnableConfig
from .llm_config import llm_docker, strip_markdown_wrapper, RETRY_CONFIGS, FALLBACK_PROMPTS
from graph.llm_retry import invoke_with_retry
from tools.example_bank import fetch_reference_examples, format_examples_for_prompt
from tools.template_store import match_template, fill_template


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


def _get_dockerfile_path(build_context: str) -> str:
    """Generate the dockerfile path from build context.
    
    Examples:
    - "." -> "Dockerfile"
    - "" -> "Dockerfile"
    - "client" -> "client/Dockerfile"
    - "./client" -> "client/Dockerfile"
    """
    normalized = _normalize_ctx(build_context)
    if normalized in (".", ""):
        return "Dockerfile"
    return f"{normalized}/Dockerfile"


def _strip_healthcheck_instructions(content: str) -> str:
    """Remove HEALTHCHECK instructions (including multi-line continuations)."""
    if not content:
        return content

    lines = content.splitlines()
    kept: list[str] = []
    skipping_continuation = False

    for line in lines:
        stripped = line.lstrip()
        if not skipping_continuation and stripped.upper().startswith("HEALTHCHECK"):
            skipping_continuation = line.rstrip().endswith("\\")
            continue
        if skipping_continuation:
            skipping_continuation = line.rstrip().endswith("\\")
            continue
        kept.append(line)

    result = "\n".join(kept)
    if content.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def _build_deterministic_dockerfile(
    service: dict[str, Any],
    stack_tokens: list[str],
    available_scripts: list[str],
    command_hints: dict[str, str] | None = None,
) -> str:
    """Generate a deterministic production-ready baseline Dockerfile."""
    tokens = {str(token).lower() for token in stack_tokens if isinstance(token, str)}
    scripts = {str(script).strip().lower() for script in available_scripts}
    port = int(service.get("port", 8000) or 8000)
    hints = command_hints or {}

    install_hint = str(hints.get("install", "") or "").strip()
    build_hint = str(hints.get("build", "") or "").strip()
    run_hint = str(hints.get("run", "") or "").strip()

    if install_hint.startswith("#"):
        install_hint = ""
    if build_hint.startswith("#"):
        build_hint = ""
    if run_hint.startswith("#"):
        run_hint = ""

    if "python" in tokens:
        python_cmd = run_hint or f"python -m uvicorn main:app --host 0.0.0.0 --port {port}"
        py_parts = [part for part in python_cmd.split(" ") if part]
        if py_parts:
            cmd_json = "[" + ", ".join(f'\"{part}\"' for part in py_parts) + "]"
        else:
            cmd_json = f'["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "{port}"]'
        return (
            "FROM python:3.11-slim\n\n"
            "WORKDIR /app\n"
            "ENV PYTHONDONTWRITEBYTECODE=1\n"
            "ENV PYTHONUNBUFFERED=1\n\n"
            "COPY requirements*.txt ./\n"
            f"RUN {install_hint or 'pip install --no-cache-dir -r requirements.txt'}\n\n"
            "COPY . .\n"
            f"EXPOSE {port}\n"
            "RUN useradd -m appuser\n"
            "USER appuser\n"
            f"CMD {cmd_json}\n"
        )

    install_cmd = install_hint or "npm ci"
    if not install_hint:
        if "pnpm" in tokens:
            install_cmd = "corepack enable pnpm && pnpm i --frozen-lockfile"
        elif "yarn" in tokens:
            install_cmd = "yarn install --frozen-lockfile"

    run_cmd = 'CMD ["npm", "start"]'
    if run_hint:
        run_parts = [part for part in run_hint.split(" ") if part]
        if run_parts:
            run_cmd = "CMD [" + ", ".join(f'\"{part}\"' for part in run_parts) + "]"
    else:
        if "start" in scripts and "pnpm" in tokens:
            run_cmd = 'CMD ["pnpm", "start"]'
        elif "start" in scripts and "yarn" in tokens:
            run_cmd = 'CMD ["yarn", "start"]'
        elif "dev" in scripts and "pnpm" in tokens:
            run_cmd = 'CMD ["pnpm", "dev"]'
        elif "dev" in scripts and "yarn" in tokens:
            run_cmd = 'CMD ["yarn", "dev"]'
        elif "dev" in scripts:
            run_cmd = 'CMD ["npm", "run", "dev"]'

    maybe_build = ""
    if build_hint:
        maybe_build = f"RUN {build_hint}\n"
    elif "build" in scripts:
        if "pnpm" in tokens:
            maybe_build = "RUN pnpm build\n"
        elif "yarn" in tokens:
            maybe_build = "RUN yarn build\n"
        else:
            maybe_build = "RUN npm run build\n"

    deps_copy_commands = (
        "COPY package*.json ./\n"
        "COPY pnpm-lock.yaml* ./\n"
        "COPY pnpm-workspace.yaml* ./\n"
        "COPY yarn.lock* ./\n"
    )
    
    # In a nested build context, the deps stage must have the nested package.json 
    # to filter/install correctly. We copy it into the proper nested directory.
    build_ctx_norm = _normalize_ctx(service.get("build_context", ".") or ".")
    if build_ctx_norm != ".":
        deps_copy_commands += f"COPY {build_ctx_norm}/package.json ./{build_ctx_norm}/package.json\n"

    return (
        "FROM node:20-alpine AS base\n"
        "WORKDIR /app\n"
        "RUN addgroup -S app && adduser -S app -G app\n\n"
        "FROM base AS deps\n"
        f"{deps_copy_commands}"
        f"RUN {install_cmd}\n\n"
        "FROM deps AS build\n"
        "COPY . .\n"
        f"{maybe_build}"
        "FROM base AS runner\n"
        "COPY --from=build /app /app\n"
        "USER app\n"
        f"EXPOSE {port}\n"
        f"{run_cmd}\n"
    )


def _repair_dockerfile_output(
    content: str,
    service: dict[str, Any],
    key_files: dict[str, Any],
    available_scripts: list[str],
) -> str:
    """Apply deterministic fixes for common monorepo/docker pitfalls in generated Dockerfiles."""
    if not content or not content.strip():
        return content

    fixed = _strip_healthcheck_instructions(content)
    changed = fixed != content

    build_ctx = _normalize_ctx(str(service.get("build_context", ".") or "."))
    has_root_pnpm_lock = isinstance(key_files, dict) and "pnpm-lock.yaml" in key_files
    has_workspace_manifest = isinstance(key_files, dict) and "pnpm-workspace.yaml" in key_files
    has_root_package = isinstance(key_files, dict) and "package.json" in key_files
    has_workspace_packages = (
        (isinstance(key_files, dict) and any(str(path).startswith("packages/") for path in key_files.keys()))
        or bool(key_files.get("__has_packages_dir__"))
    )

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

    # In pnpm monorepos, install dependencies with workspace context from repo root.
    if has_root_pnpm_lock and build_ctx != "." and "pnpm i --frozen-lockfile" in fixed:
        copy_lines: list[str] = []
        if has_root_package:
            copy_lines.append("COPY package.json ./")
        copy_lines.append("COPY pnpm-lock.yaml* ./")
        if has_workspace_manifest:
            copy_lines.append("COPY pnpm-workspace.yaml* ./")
        # We ensure the path to the workspace file exists by doing a specific copy, but
        # because docker COPY fails if the target directory doesn't exist, we must mkdir it first.
        copy_lines.append(f"RUN mkdir -p {build_ctx}")
        copy_lines.append(f"COPY {build_ctx}/package.json {build_ctx}/package.json")
        if has_workspace_packages:
            copy_lines.append("COPY packages ./packages")

        copy_block = "\n".join(copy_lines)
        copy_pattern = rf"(?im)^\s*COPY\s+(?:{re.escape(build_ctx)}/package\.json\s+)?pnpm-lock\.yaml\*\s*(?:pnpm-workspace\.yaml\*\s*)?\./\s*$"
        new_fixed = re.sub(copy_pattern, copy_block, fixed)
        
        # fallback if pattern doesn't match the exact deterministic builder variant natively
        if new_fixed == fixed and "node:20-alpine" in fixed:
            new_fixed = re.sub(
                r"(?im)^\s*COPY\s+package\*?\.json\s+\./\s*\n\s*COPY\s+pnpm-lock\.yaml\*\s+\./\s*\n\s*COPY\s+yarn\.lock\*\s+\./\s*\n(?:^\s*COPY\s+[\w/.-]+\s+[\w/.-]+\s*\n)*", 
                copy_block + "\n", 
                fixed
            )
            
        if new_fixed != fixed:
            fixed = new_fixed
            changed = True

        install_pattern = r"(?im)corepack\s+enable\s+pnpm\s*&&\s*pnpm\s+i\s+--frozen-lockfile"
        new_fixed = re.sub(install_pattern, f"corepack enable pnpm && pnpm i --frozen-lockfile --filter ./{build_ctx}...", fixed)
        if new_fixed != fixed:
            fixed = new_fixed
            changed = True

    scripts_set = {s.strip() for s in available_scripts}

    # Normalize workspace-name filters to path-based filters and remove accidental duplicates.
    if build_ctx != ".":
        # Prefer a stable path filter for monorepo service installs.
        new_fixed = re.sub(
            r"(?im)(pnpm\s+i\s+--frozen-lockfile)\s+--filter\s+@[\w./-]+\.\.\.",
            rf"\1 --filter ./{build_ctx}...",
            fixed,
        )
        if new_fixed != fixed:
            fixed = new_fixed
            changed = True

        # Collapse duplicate identical filter flags produced by model output.
        filter_expr = re.escape(f"--filter ./{build_ctx}...")
        new_fixed = re.sub(
            rf"(?im)({filter_expr})(?:\s+{filter_expr})+",
            f"--filter ./{build_ctx}...",
            fixed,
        )
        if new_fixed != fixed:
            fixed = new_fixed
            changed = True
            
        # Detect Turborepo and use `turbo run build --filter=<service_dir>...`
        has_turbo = isinstance(key_files, dict) and ("turbo.json" in key_files or "turbo.yaml" in key_files)
        if has_turbo and ("build" in scripts_set or "turbo" in fixed.lower() or "pnpm run build" in fixed or "pnpm build" in fixed):
            # Match standalone turbo commands
            new_fixed = re.sub(
                r"(?im)^\s*RUN\s+(?:npx\s+(?:--yes\s+)?)?turbo\s+(?:run\s+)?build\s*$", 
                f"RUN npx --yes turbo run build --filter=./{build_ctx}...", 
                fixed
            )
            # Match standalone pnpm/npm/yarn build commands
            if new_fixed == fixed:
                new_fixed = re.sub(
                    r"(?im)^\s*RUN\s+(?:pnpm|npm|yarn)\s+(?:run\s+)?build\s*$", 
                    f"RUN npx --yes turbo run build --filter=./{build_ctx}...", 
                    fixed
                )
            # Match compound commands like: RUN corepack enable pnpm && pnpm run build
            if new_fixed == fixed:
                new_fixed = re.sub(
                    r"(?im)(^\s*RUN\s+.+&&\s*)(?:pnpm|npm|yarn)\s+(?:run\s+)?build\s*$", 
                    f"\\1npx --yes turbo run build --filter=./{build_ctx}...", 
                    fixed
                )
            if new_fixed != fixed:
                fixed = new_fixed
                changed = True

    # If workspace packages exist and deps stage installs via pnpm, include packages in deps context.
    if has_workspace_packages and "pnpm i --frozen-lockfile" in fixed and "COPY packages ./packages" not in fixed:
        deps_copy_match = re.search(
            rf"(?im)^\s*COPY\s+{re.escape(build_ctx)}/package\.json\s+{re.escape(build_ctx)}/package\.json\s*$",
            fixed,
        )
        if deps_copy_match:
            insert_at = deps_copy_match.end()
            new_fixed = fixed[:insert_at] + "\nCOPY packages ./packages" + fixed[insert_at:]
            if new_fixed != fixed:
                fixed = new_fixed
                changed = True

    # Never swallow build failures with `|| true` in production Dockerfiles.
    new_fixed = re.sub(r"(?im)^\s*RUN\s+((?:pnpm|npm|yarn)\s+(?:run\s+)?build)\s*\|\|\s*true\s*$", r"RUN \1", fixed)
    if new_fixed != fixed:
        fixed = new_fixed
        changed = True

    # If no build script exists, drop pnpm build line to avoid missing-script failures.
    if "build" not in scripts_set:
        new_fixed = re.sub(r"(?im)^\s*RUN\s+(?:pnpm|npm|yarn)\s+(?:run\s+)?build\s*$\n?", "", fixed)
        if new_fixed != fixed:
            fixed = new_fixed
            changed = True

    # Ensure backend runner copies node_modules from deps root, not nested app path.
    if build_ctx != ".":
        nested_nm = rf"/app/{re.escape(build_ctx)}/node_modules"
        new_fixed = re.sub(nested_nm, "/app/node_modules", fixed)
        if new_fixed != fixed:
            fixed = new_fixed
            changed = True

    # If builder stage switches to service workdir but omits build command, insert a guarded build step.
    if build_ctx != "." and f"WORKDIR /app/{build_ctx}" in fixed:
        runner_boundary = re.search(r"(?im)^FROM\s+base\s+AS\s+runner\s*$", fixed)
        has_build_run = bool(re.search(r"(?im)^\s*RUN\s+.*pnpm\s+build", fixed))
        if runner_boundary and not has_build_run:
            insert_at = runner_boundary.start()
            build_snippet = (
                "\nENV NODE_ENV=production\n"
                "RUN if [ -f package.json ] && grep -q '\"build\"' package.json; then \\\n"
                "      pnpm build; \\\n"
                "    fi\n"
            )
            new_fixed = fixed[:insert_at] + build_snippet + fixed[insert_at:]
            if new_fixed != fixed:
                fixed = new_fixed
                changed = True

    # Ensure stage declarations are on their own lines (can be broken by aggressive substitutions).
    new_fixed = re.sub(
        r"(?im)([^\n])\s+(FROM\s+(?:node:[^\s]+|base)\s+AS\s+\w+)",
        r"\1\n\2",
        fixed,
    )
    if new_fixed != fixed:
        fixed = new_fixed
        changed = True

    # Normalize shell-form node CMD to JSON exec form for better signal handling.
    cmd_match = re.search(r"(?im)^\s*CMD\s+node\s+([^\n]+?)\s*$", fixed)
    if cmd_match:
        arg = cmd_match.group(1).strip().strip('"').strip("'")
        if arg and not arg.startswith("["):
            new_fixed = re.sub(
                r"(?im)^\s*CMD\s+node\s+[^\n]+\s*$",
                f'CMD ["node", "{arg}"]',
                fixed,
                count=1,
            )
            if new_fixed != fixed:
                fixed = new_fixed
                changed = True

    # Prefer npm/pnpm script entrypoint for backend services when `start` exists.
    service_name = str(service.get("name", "")).strip().lower()
    is_backend_like = any(token in service_name for token in ("backend", "api", "server"))
    if is_backend_like and "start" in scripts_set:
        new_fixed = re.sub(
            r'(?im)^\s*CMD\s*\[\s*"node"\s*,\s*"[^"]+"\s*\]\s*$',
            'CMD ["pnpm", "start"]',
            fixed,
            count=1,
        )
        if new_fixed != fixed:
            fixed = new_fixed
            changed = True

        new_fixed = re.sub(
            r"(?im)^\s*CMD\s+node\s+[^\n]+\s*$",
            'CMD ["pnpm", "start"]',
            fixed,
            count=1,
        )
        if new_fixed != fixed:
            fixed = new_fixed
            changed = True

    return fixed if changed else content


def dockerfile_generator_node(state: Dict[str, Any], config: RunnableConfig = None) -> Dict[str, Any]:
    """Generate production Dockerfiles for each service."""
    scan = state.get("repo_scan", {})
    key_files = scan.get("key_files", {})
    scan_dirs = scan.get("dirs", []) if isinstance(scan, dict) else []
    services = state.get("services", [])
    commands = state.get("commands", {}) if isinstance(state.get("commands", {}), dict) else {}
    command_map = commands.get("by_service", {}) if isinstance(commands, dict) else {}
    
    dockerfiles = {}
    warnings = state.get("docker_generation_warnings", [])
    if not isinstance(warnings, list):
        warnings = []
    
    for service in services:
        original_ctx = service.get("build_context", ".")
        dockerfile_path = service.get("dockerfile_path", "")
        if original_ctx == "." and dockerfile_path and "/" in dockerfile_path:
            service["build_context"] = "/".join(dockerfile_path.split("/")[:-1])
            
        svc_name = service["name"]
        build_ctx = service["build_context"]
        port = service["port"]
        dockerfile_key = dockerfile_path or _get_dockerfile_path(build_ctx)
        available_scripts = _extract_package_scripts(key_files, build_ctx)
        command_hints = command_map.get(svc_name, {}) if isinstance(command_map, dict) else {}
        if not isinstance(command_hints, dict):
            command_hints = {}
        scripts_hint = (
            f"Detected package.json scripts for this service: {', '.join(available_scripts)}"
            if available_scripts
            else "Detected package.json scripts for this service: none/unknown"
        )
        commands_hint = (
            "Command hints from commands_gen:\n"
            f"- install: {command_hints.get('install', 'n/a')}\n"
            f"- build: {command_hints.get('build', 'n/a')}\n"
            f"- run: {command_hints.get('run', 'n/a')}"
        )
        
        # Look up the pre-existing Dockerfile using the planner-provided path
        existing_dockerfile = None
        if dockerfile_path:
            existing_dockerfile = key_files.get(dockerfile_path)
        
        # ─── Template-first flow ──────────────────────────────────────────
        # 1. Try matching a Supabase template
        # 2. Fall back to deterministic builder
        # 3. Fall back to existing Dockerfile from repo
        template_used = False
        stack_tokens = list(state.get("stack_tokens", []))
        
        # Build signals for template matching — infer from key_files evidence
        has_pnpm_lock = isinstance(key_files, dict) and "pnpm-lock.yaml" in key_files
        has_workspace_yaml = isinstance(key_files, dict) and "pnpm-workspace.yaml" in key_files
        is_monorepo = (
            build_ctx != "."
            or (original_ctx == "." and dockerfile_path and "/" in dockerfile_path)
            or has_workspace_yaml
        )
        
        # Detect Turborepo from turbo.json OR root package.json scripts containing "turbo"
        has_turbo = isinstance(key_files, dict) and ("turbo.json" in key_files or "turbo.yaml" in key_files)
        if not has_turbo and isinstance(key_files, dict):
            root_pkg = key_files.get("package.json", "")
            if isinstance(root_pkg, str) and "turbo" in root_pkg.lower():
                has_turbo = True
        
        # Detect standalone Next.js output
        has_standalone = False
        svc_pkg_path = f"{build_ctx}/package.json" if build_ctx != "." else "package.json"
        svc_pkg_content = key_files.get(svc_pkg_path, "") if isinstance(key_files, dict) else ""
        if isinstance(svc_pkg_content, str) and '"standalone"' in svc_pkg_content.lower():
            has_standalone = True
        for cfg_name in [f"{build_ctx}/next.config.js", f"{build_ctx}/next.config.mjs", f"{build_ctx}/next.config.ts"]:
            cfg_content = key_files.get(cfg_name, "") if isinstance(key_files, dict) else ""
            if isinstance(cfg_content, str) and "standalone" in cfg_content:
                has_standalone = True
                break
        
        # Enrich stack tokens with inferred evidence so the template scorer can match
        token_set = {t.lower() for t in stack_tokens}
        if has_pnpm_lock and "pnpm" not in token_set:
            stack_tokens.append("pnpm")
        if has_turbo and "turbo" not in token_set:
            stack_tokens.append("turbo")
        if isinstance(svc_pkg_content, str) and "next" in svc_pkg_content.lower() and "next" not in token_set:
            stack_tokens.append("next")
        
        template_signals = {
            "is_monorepo": is_monorepo,
            "has_turbo": has_turbo,
            "has_standalone": has_standalone,
        }
        
        print(f"[docker_gen] Matching template for {svc_name}: tokens={stack_tokens}, signals={template_signals}")
        
        matched = match_template(stack_tokens, template_signals)
        if matched:
            template_vars = dict(matched.get("variables", {}))
            # Override with actual service values
            template_vars["port"] = port
            template_vars["service_path"] = build_ctx if build_ctx != "." else template_vars.get("service_path", ".")
            
            baseline_dockerfile = fill_template(matched["template_content"], template_vars)
            template_used = True
            print(f"[docker_gen] Template matched: {matched.get('name', 'unknown')} for {svc_name}")
        elif isinstance(existing_dockerfile, str) and existing_dockerfile.strip():
            baseline_dockerfile = existing_dockerfile
        else:
            baseline_dockerfile = _build_deterministic_dockerfile(
                service,
                stack_tokens,
                available_scripts,
                command_hints=command_hints,
            )
        
        repair_key_files = dict(key_files) if isinstance(key_files, dict) else {}
        if isinstance(scan_dirs, list):
            repair_key_files["__has_packages_dir__"] = any(
                str(directory).strip().startswith("packages") for directory in scan_dirs
            )
        baseline_dockerfile = _repair_dockerfile_output(
            baseline_dockerfile,
            service=service,
            key_files=repair_key_files,
            available_scripts=available_scripts,
        )

        # ─── Use the baseline directly as the final Dockerfile ─────────────
        # The LLM is only asked to verify, not to rewrite.
        dockerfiles[dockerfile_key] = baseline_dockerfile

        # ─── LLM Verification (advisory only, does not replace content) ───
        verify_prompt = f"""
You are a DevOps expert reviewing a Dockerfile for production deployment.
Do NOT output a new Dockerfile. Only check for issues.

Service: {svc_name}
Build context: {build_ctx}
Port: {port}
Stack: {state.get('detected_stack', 'unknown')}
{scripts_hint}
{commands_hint}
{'Template used: ' + matched.get('name', 'unknown') if template_used and matched else 'Generated deterministically'}

Dockerfile to verify:
{baseline_dockerfile}

{('EXISTING Dockerfile (from repository) for reference:' + chr(10) + existing_dockerfile) if existing_dockerfile else ''}

Respond with ONLY a JSON object:
- "verdict": "pass" if the Dockerfile looks correct, "fail" if there are critical issues
- "issues": a list of strings describing each issue (empty list if verdict is "pass")

Example: {{"verdict": "pass", "issues": []}}
Example: {{"verdict": "fail", "issues": ["Wrong port exposed", "Missing build step"]}}
"""

        try:
            response, _, _ = invoke_with_retry(
                invoke_fn=lambda raw_prompt: llm_docker.invoke(raw_prompt, config=config),
                prompt=verify_prompt,
                fallback_prompt=FALLBACK_PROMPTS["docker"],
                config=RETRY_CONFIGS["docker"],
                node_name=f"docker_verify:{svc_name}",
            )
            verify_text = strip_markdown_wrapper(response.content).strip()
            try:
                verdict = json.loads(verify_text)
            except (json.JSONDecodeError, TypeError):
                # Try to recover if the model wrapped JSON in extra text or formatting.
                try:
                    import re
                    match = re.search(r"\{.*\}", verify_text, re.DOTALL)
                    if match:
                        verdict = json.loads(match.group(0))
                    else:
                        raise json.JSONDecodeError("no JSON object could be decoded", verify_text, 0)
                except Exception:
                    warnings.append(f"llm_verify_unparsed:{svc_name}")
                    print(f"[docker_verify] {svc_name}: Could not parse LLM verification response")
                    verdict = None

            if isinstance(verdict, dict):
                if verdict.get("verdict") == "fail":
                    issues = verdict.get("issues", [])
                    for issue in issues:
                        warnings.append(f"llm_verify:{svc_name}:{issue}")
                    print(f"[docker_verify] {svc_name}: FAIL — {issues}")
                else:
                    print(f"[docker_verify] {svc_name}: PASS")
        except Exception as e:
            warnings.append(f"llm_verify_failed:{svc_name}:{e}")
        finally:
            service["build_context"] = original_ctx
    
    state["dockerfiles"] = dockerfiles
    if warnings:
        state["docker_generation_warnings"] = warnings
    return state
