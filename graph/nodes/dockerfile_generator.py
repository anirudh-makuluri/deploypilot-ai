from typing import Dict, Any
import json
import re
from pydantic import BaseModel, Field
from langchain_core.runnables.config import RunnableConfig
from tools.template_store import match_template, fill_template


class DockerfilePlan(BaseModel):
    runtime: str = Field(description="node or python")
    base_image: str = Field(description="Base image for build/runtime")
    workdir: str = Field(default="/app")
    install_cmd: str = Field(default="")
    build_cmd: str = Field(default="")
    run_cmd: str = Field(default="")
    package_manager: str = Field(default="")
    expose_port: int = Field(default=8000)
    use_non_root_user: bool = Field(default=True)


def _build_plan_defaults(
    service: dict[str, Any],
    stack_tokens: list[str],
    available_scripts: list[str],
    command_hints: dict[str, str] | None = None,
) -> dict[str, Any]:
    tokens = {str(token).lower() for token in stack_tokens if isinstance(token, str)}
    scripts = {str(script).strip().lower() for script in available_scripts}
    hints = command_hints or {}
    port = int(service.get("port", 8000) or 8000)

    if "python" in tokens:
        return {
            "runtime": "python",
            "base_image": "python:3.11-slim",
            "workdir": "/app",
            "install_cmd": str(hints.get("install") or "pip install --no-cache-dir -r requirements.txt"),
            "build_cmd": "",
            "run_cmd": str(hints.get("run") or f"python -m uvicorn main:app --host 0.0.0.0 --port {port}"),
            "package_manager": "pip",
            "expose_port": port,
            "use_non_root_user": True,
        }

    if "pnpm" in tokens:
        install_cmd = str(hints.get("install") or "corepack enable pnpm && pnpm i --frozen-lockfile")
        package_manager = "pnpm"
    elif "yarn" in tokens:
        install_cmd = str(hints.get("install") or "yarn install --frozen-lockfile")
        package_manager = "yarn"
    else:
        install_cmd = str(hints.get("install") or "npm ci")
        package_manager = "npm"

    if hints.get("build"):
        build_cmd = str(hints.get("build"))
    elif "build" in scripts:
        build_cmd = f"{package_manager} {'run ' if package_manager == 'npm' else ''}build".strip()
    else:
        build_cmd = ""

    if hints.get("run"):
        run_cmd = str(hints.get("run"))
    elif "start" in scripts:
        run_cmd = "npm start" if package_manager == "npm" else f"{package_manager} start"
    elif "vite" in scripts:
        # Vite production mode (standard approach, used by Vercel)
        run_cmd = "npm vite" if package_manager == "npm" else f"{package_manager} vite"
    elif "dev" in scripts:
        run_cmd = "npm run dev" if package_manager == "npm" else f"{package_manager} dev"
    else:
        run_cmd = "npm start"

    return {
        "runtime": "node",
        "base_image": "node:20-alpine",
        "workdir": "/app",
        "install_cmd": install_cmd,
        "build_cmd": build_cmd,
        "run_cmd": run_cmd,
        "package_manager": package_manager,
        "expose_port": port,
        "use_non_root_user": True,
    }


def _merge_plan(defaults: dict[str, Any], llm_plan: dict[str, Any]) -> DockerfilePlan:
    merged = dict(defaults)
    for key, value in (llm_plan or {}).items():
        if value in (None, ""):
            continue
        merged[key] = value
    return DockerfilePlan(**merged)


def _render_dockerfile_from_plan(plan: DockerfilePlan, service: dict[str, Any]) -> str:
    build_ctx_norm = _normalize_ctx(service.get("build_context", ".") or ".")
    dockerfile_path_norm = _normalize_ctx(service.get("dockerfile_path", "") or "")
    service_subpath = ""
    if (
        build_ctx_norm == "."
        and dockerfile_path_norm not in ("", ".", "Dockerfile")
        and dockerfile_path_norm.endswith("/Dockerfile")
    ):
        service_subpath = dockerfile_path_norm[: -len("/Dockerfile")]
    if plan.runtime.lower() == "python":
        run_parts = [part for part in str(plan.run_cmd).split(" ") if part]
        cmd_json = "[" + ", ".join(f'"{part}"' for part in run_parts) + "]" if run_parts else '["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]'
        non_root = "RUN useradd -m appuser\nUSER appuser\n" if plan.use_non_root_user else ""
        return (
            f"FROM {plan.base_image}\n\n"
            f"WORKDIR {plan.workdir}\n"
            "ENV PYTHONDONTWRITEBYTECODE=1\n"
            "ENV PYTHONUNBUFFERED=1\n\n"
            "COPY requirements*.txt ./\n"
            f"RUN {plan.install_cmd}\n\n"
            "COPY . .\n"
            f"EXPOSE {int(plan.expose_port)}\n"
            f"{non_root}"
            f"CMD {cmd_json}\n"
        )

    if build_ctx_norm == ".":
        deps_copy_commands = (
            "COPY package*.json ./\n"
            "COPY pnpm-lock.yaml* ./\n"
            "COPY pnpm-workspace.yaml* ./\n"
            "COPY yarn.lock* ./\n"
        )
        if service_subpath:
            deps_copy_commands += f"COPY {service_subpath}/package.json {service_subpath}/package.json\n"
    else:
        # Scoped contexts can only COPY files from within that context.
        deps_copy_commands = "COPY package*.json ./\n"

    run_parts = [part for part in str(plan.run_cmd).split(" ") if part]
    run_cmd = "CMD [" + ", ".join(f'"{part}"' for part in run_parts) + "]" if run_parts else 'CMD ["npm", "start"]'
    if service_subpath:
        run_cmd = f'CMD ["pnpm", "--filter", "./{service_subpath}", "start"]'
    maybe_build = f"RUN {plan.build_cmd}\n" if str(plan.build_cmd).strip() else ""
    if service_subpath and not str(plan.build_cmd).strip():
        maybe_build = f"RUN pnpm --filter ./{service_subpath}... build\n"
    user_bits = "RUN addgroup -S app && adduser -S app -G app\n" if plan.use_non_root_user else ""
    final_user = "USER app\n" if plan.use_non_root_user else ""
    return (
        f"FROM {plan.base_image} AS base\n"
        f"WORKDIR {plan.workdir}\n"
        f"{user_bits}\n"
        "FROM base AS deps\n"
        f"{deps_copy_commands}"
        f"RUN {plan.install_cmd}\n\n"
        "FROM deps AS build\n"
        "COPY . .\n"
        f"{maybe_build}"
        "FROM base AS runner\n"
        f"WORKDIR {plan.workdir}\n"
        f"COPY --from=build {plan.workdir} {plan.workdir}\n"
        f"{final_user}"
        f"EXPOSE {int(plan.expose_port)}\n"
        f"{run_cmd}\n"
    )


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
    while "/./" in normalized:
        normalized = normalized.replace("/./", "/")
    if normalized.endswith("/."):
        normalized = normalized[:-2]
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
    build_ctx_norm = _normalize_ctx(service.get("build_context", ".") or ".")
    dockerfile_path_norm = _normalize_ctx(service.get("dockerfile_path", "") or "")
    service_subpath = ""
    if (
        build_ctx_norm == "."
        and dockerfile_path_norm not in ("", ".", "Dockerfile")
        and dockerfile_path_norm.endswith("/Dockerfile")
    ):
        service_subpath = dockerfile_path_norm[: -len("/Dockerfile")]
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

    if build_ctx_norm == ".":
        deps_copy_commands = (
            "COPY package*.json ./\n"
            "COPY pnpm-lock.yaml* ./\n"
            "COPY pnpm-workspace.yaml* ./\n"
            "COPY yarn.lock* ./\n"
        )
        if service_subpath:
            deps_copy_commands += f"COPY {service_subpath}/package.json {service_subpath}/package.json\n"
    else:
        # Scoped contexts can only COPY files from within that context.
        deps_copy_commands = "COPY package*.json ./\n"

    if service_subpath and "pnpm i --frozen-lockfile" in install_cmd and "--filter" not in install_cmd:
        install_cmd = f"{install_cmd} --filter ./{service_subpath}..."
    if service_subpath:
        run_cmd = f'CMD ["pnpm", "--filter", "./{service_subpath}", "start"]'
        if not maybe_build.strip():
            maybe_build = f"RUN pnpm --filter ./{service_subpath}... build\n"

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
        "WORKDIR /app\n"
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
    dockerfile_path_norm = _normalize_ctx(str(service.get("dockerfile_path", "") or ""))
    service_subpath = ""
    if (
        build_ctx == "."
        and dockerfile_path_norm not in ("", ".", "Dockerfile")
        and dockerfile_path_norm.endswith("/Dockerfile")
    ):
        service_subpath = dockerfile_path_norm[: -len("/Dockerfile")]
    has_root_pnpm_lock = isinstance(key_files, dict) and "pnpm-lock.yaml" in key_files
    has_workspace_manifest = isinstance(key_files, dict) and "pnpm-workspace.yaml" in key_files
    has_root_package = isinstance(key_files, dict) and "package.json" in key_files
    has_workspace_packages = (
        (isinstance(key_files, dict) and any(str(path).startswith("packages/") for path in key_files.keys()))
        or bool(key_files.get("__has_packages_dir__"))
    )

    # Scoped build contexts (e.g. apps/dashboard) cannot access repo-root files.
    # Strip common root-level workspace copy lines regardless of install command shape.
    if build_ctx != ".":
        scoped_cleanup = [
            r"(?im)^\s*COPY\s+pnpm-lock\.yaml\*?\s+\./\s*$\n?",
            r"(?im)^\s*COPY\s+pnpm-workspace\.yaml\*?\s+\./\s*$\n?",
        ]
        for pattern in scoped_cleanup:
            new_fixed = re.sub(pattern, "", fixed)
            if new_fixed != fixed:
                fixed = new_fixed
                changed = True
        # Catch multi-source COPY variants, e.g.:
        # COPY pnpm-lock.yaml pnpm-workspace.yaml* ./
        scoped_lines: list[str] = []
        for line in fixed.splitlines():
            stripped = line.strip().lower()
            if stripped.startswith("copy ") and "--from=" not in stripped:
                if "pnpm-lock.yaml" in stripped or "pnpm-workspace.yaml" in stripped:
                    changed = True
                    continue
            scoped_lines.append(line)
        rebuilt = "\n".join(scoped_lines).strip()
        fixed = (rebuilt + "\n") if rebuilt else ""

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

    # If build context is scoped (e.g. apps/dashboard), Docker cannot access repo-root
    # workspace files. Avoid injecting root-level pnpm workspace COPY lines.
    if has_root_pnpm_lock and build_ctx != "." and "pnpm i --frozen-lockfile" in fixed:
        # Drop monorepo root copy patterns that are invalid under scoped build contexts.
        scoped_cleanup = [
            r"(?im)^\s*COPY\s+pnpm-workspace\.yaml\*?\s+\./\s*$\n?",
            r"(?im)^\s*COPY\s+pnpm-lock\.yaml\*?\s+\./\s*$\n?",
            r"(?im)^\s*COPY\s+package\.json\s+\./\s*$\n?",
            rf"(?im)^\s*COPY\s+{re.escape(build_ctx)}/package\.json\s+{re.escape(build_ctx)}/package\.json\s*$\n?",
            rf"(?im)^\s*RUN\s+mkdir\s+-p\s+{re.escape(build_ctx)}\s*$\n?",
        ]
        for pattern in scoped_cleanup:
            new_fixed = re.sub(pattern, "", fixed)
            if new_fixed != fixed:
                fixed = new_fixed
                changed = True

        # Ensure deps stage still has at least local package.json copy.
        if "COPY package*.json ./" not in fixed and "COPY package.json ./" not in fixed:
            new_fixed = re.sub(
                r"(?im)^(\s*FROM\s+base\s+AS\s+deps\s*$)",
                r"\1\nCOPY package*.json ./",
                fixed,
                count=1,
            )
            if new_fixed != fixed:
                fixed = new_fixed
                changed = True

        # Keep install command context-local (no --filter ./apps/... when already scoped).
        new_fixed = re.sub(
            r"(?im)corepack\s+enable\s+pnpm\s*&&\s*pnpm\s+i\s+--frozen-lockfile\s+--filter\s+\./[^\s]+",
            "corepack enable pnpm && pnpm i --frozen-lockfile",
            fixed,
        )
        if new_fixed != fixed:
            fixed = new_fixed
            changed = True

    # In pnpm monorepos rooted at ".", install with workspace filter context.
    if has_root_pnpm_lock and build_ctx == "." and "pnpm i --frozen-lockfile" in fixed:
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

        filter_target = service_subpath or build_ctx
        install_pattern = r"(?im)corepack\s+enable\s+pnpm\s*&&\s*pnpm\s+i\s+--frozen-lockfile(?:\s+--filter\s+\./[^\s]+)*"
        replacement = "corepack enable pnpm && pnpm i --frozen-lockfile"
        if filter_target and filter_target != ".":
            replacement += f" --filter ./{filter_target}..."
        new_fixed = re.sub(install_pattern, replacement, fixed)
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

    # Hard guard: never keep malformed current-dir pnpm filters.
    new_fixed = re.sub(r"(?im)\s+--filter\s+\./\.\.\.\.", "", fixed)
    if new_fixed != fixed:
        fixed = new_fixed
        changed = True

    # Collapse duplicate identical pnpm filter flags anywhere in the file.
    new_fixed = re.sub(
        r"(?im)(--filter\s+\./[^\s]+\.\.\.)(?:\s+\1)+",
        r"\1",
        fixed,
    )
    if new_fixed != fixed:
        fixed = new_fixed
        changed = True
    new_fixed = re.sub(r"(?im)\s+--filter\s+\./\.\.\.", "", fixed)
    if new_fixed != fixed:
        fixed = new_fixed
        changed = True

    # Ensure runner workdir is service path for nested monorepo Dockerfiles.
    if service_subpath and "FROM base AS runner" in fixed and f"WORKDIR /app/{service_subpath}" not in fixed:
        new_fixed = re.sub(
            r"(?im)^(\s*FROM\s+base\s+AS\s+runner\s*$)",
            rf"\1\nWORKDIR /app/{service_subpath}",
            fixed,
            count=1,
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

    # Ensure runtime files are writable for non-root app user.
    if re.search(r"(?im)^\s*USER\s+app\s*$", fixed):
        # If runtime command uses pnpm, ensure pnpm is enabled in runner stage too.
        uses_pnpm_cmd = bool(re.search(r'(?im)^\s*CMD\s+\[\s*"pnpm"\s*,', fixed))
        has_runner_corepack = bool(re.search(r"(?im)^\s*RUN\s+corepack\s+enable\s+pnpm\s*$", fixed))
        if uses_pnpm_cmd and not has_runner_corepack:
            user_match = re.search(r"(?im)^\s*USER\s+app\s*$", fixed)
            if user_match:
                insert_at = user_match.start()
                new_fixed = fixed[:insert_at] + "RUN corepack enable pnpm\n" + fixed[insert_at:]
                if new_fixed != fixed:
                    fixed = new_fixed
                    changed = True

        has_chown = bool(re.search(r"(?im)^\s*RUN\s+chown\s+-R\s+app:app\s+/app\s*$", fixed))
        if not has_chown:
            user_match = re.search(r"(?im)^\s*USER\s+app\s*$", fixed)
            if user_match:
                insert_at = user_match.start()
                new_fixed = fixed[:insert_at] + "RUN chown -R app:app /app\n" + fixed[insert_at:]
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

    # Final guard: remove COPY sources that cannot exist under this build context.
    if isinstance(key_files, dict):
        known_files = {_normalize_ctx(str(p)) for p in key_files.keys()}
        known_dirs = {
            _normalize_ctx(str(p).split("/", 1)[0]) if "/" in str(p) else _normalize_ctx(str(p))
            for p in key_files.keys()
        }
        valid_lines: list[str] = []
        for line in fixed.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("copy ") and "--from=" not in stripped.lower():
                parts = re.split(r"\s+", stripped[5:].strip())
                src = ""
                if parts and not parts[0].startswith("--") and len(parts) >= 2:
                    src = parts[0]
                if src:
                    src_norm = _normalize_ctx(src.strip('"').strip("'"))
                    candidate = _normalize_ctx(f"{build_ctx}/{src_norm}") if build_ctx != "." else src_norm
                    if src_norm != "." and src_norm != "" and candidate not in known_files:
                        if not any(k.startswith(candidate + "/") for k in known_files | known_dirs):
                            changed = True
                            continue
            valid_lines.append(line)
        fixed = "\n".join(valid_lines).strip() + "\n"

    return fixed if changed else content


def _service_subpath(service: dict[str, Any]) -> str:
    build_ctx_norm = _normalize_ctx(service.get("build_context", ".") or ".")
    dockerfile_path_norm = _normalize_ctx(service.get("dockerfile_path", "") or "")
    if (
        build_ctx_norm == "."
        and dockerfile_path_norm not in ("", ".", "Dockerfile")
        and dockerfile_path_norm.endswith("/Dockerfile")
    ):
        return dockerfile_path_norm[: -len("/Dockerfile")]
    return ""


def _inject_command_hints_into_template(
    dockerfile_content: str,
    service: dict[str, Any],
    command_hints: dict[str, str] | None,
) -> str:
    """Inject commands_gen install/build/run hints into a matched template Dockerfile."""
    if not dockerfile_content.strip():
        return dockerfile_content

    hints = command_hints or {}
    install_cmd = str(hints.get("install", "") or "").strip()
    build_cmd = str(hints.get("build", "") or "").strip()
    run_cmd = str(hints.get("run", "") or "").strip()
    subpath = _service_subpath(service)

    if install_cmd.startswith("#"):
        install_cmd = ""
    if build_cmd.startswith("#"):
        build_cmd = ""
    if run_cmd.startswith("#"):
        run_cmd = ""

    lower_template = dockerfile_content.lower()
    template_uses_pnpm = ("pnpm-lock.yaml" in lower_template) or ("pnpm i" in lower_template) or ('"pnpm"' in lower_template)

    # Do not let generic npm hints downgrade pnpm-focused templates.
    if template_uses_pnpm:
        if install_cmd and "pnpm" not in install_cmd:
            install_cmd = ""
        if build_cmd and ("pnpm" not in build_cmd and "turbo" not in build_cmd):
            build_cmd = ""
        if run_cmd and "pnpm" not in run_cmd:
            run_cmd = ""

    if install_cmd and "pnpm install" in install_cmd:
        install_cmd = install_cmd.replace("pnpm install", "pnpm i")
    if install_cmd and "pnpm i --frozen-lockfile" in install_cmd and subpath and "--filter" not in install_cmd:
        install_cmd = f"{install_cmd} --filter ./{subpath}..."
    # Guard against malformed filter suffixes.
    install_cmd = install_cmd.replace("--filter ./....", "").strip()

    updated = dockerfile_content
    if install_cmd:
        updated = re.sub(
            r"(?im)^\s*RUN\s+corepack\s+enable\s+pnpm\s*&&\s*pnpm\s+i[^\n]*$",
            f"RUN corepack enable pnpm && {install_cmd}",
            updated,
            count=1,
        )
        updated = re.sub(
            r"(?im)^\s*RUN\s+pnpm\s+i[^\n]*$",
            f"RUN {install_cmd}",
            updated,
            count=1,
        )

    if build_cmd:
        updated = re.sub(
            r"(?im)^\s*RUN\s+(?:pnpm|npm|yarn)\s+(?:run\s+)?build[^\n]*$",
            f"RUN {build_cmd}",
            updated,
            count=1,
        )

    if run_cmd:
        # Avoid dev server defaults in production Dockerfiles.
        if any(token in run_cmd.lower() for token in (" dev", " run dev", "vite dev")):
            run_cmd = ""
    if run_cmd:
        run_parts = [part for part in run_cmd.split(" ") if part]
        if run_parts:
            run_json = "CMD [" + ", ".join(f'"{part}"' for part in run_parts) + "]"
            updated = re.sub(r"(?im)^\s*CMD\s+[^\n]+$", run_json, updated, count=1)

    # Ensure runner stage has a concrete workdir for monorepo subpath services.
    if subpath and "FROM base AS runner" in updated and f"WORKDIR /app/{subpath}" not in updated:
        updated = re.sub(
            r"(?im)^(\s*FROM\s+base\s+AS\s+runner\s*$)",
            rf"\1\nWORKDIR /app/{subpath}",
            updated,
            count=1,
        )

    return updated


def _apply_vite_start_fallback(
    dockerfile_content: str,
    available_scripts: list[str],
    port: int,
) -> str:
    """For Vite templates, adjust start command based on available scripts.
    
    Priority:
    1. Use 'start' if available
    2. Use 'vite' if available (standard Vercel approach)
    3. Use 'preview' as fallback
    4. Keep template default if none found
    """
    scripts = {str(s).strip().lower() for s in available_scripts}
    if "start" in scripts:
        return dockerfile_content
    
    # Prefer 'vite' (production mode) over 'preview' (development preview)
    if "vite" in scripts:
        return re.sub(
            r'(?im)^\s*CMD\s+\[[^\n]+\]\s*$',
            f'CMD ["pnpm", "vite"]',
            dockerfile_content,
            count=1,
        )
    
    if "preview" in scripts:
        return re.sub(
            r'(?im)^\s*CMD\s+\[[^\n]+\]\s*$',
            f'CMD ["pnpm", "preview", "--host", "0.0.0.0", "--port", "{int(port)}"]',
            dockerfile_content,
            count=1,
        )

    return dockerfile_content


def dockerfile_generator_node(state: Dict[str, Any], config: RunnableConfig = None) -> Dict[str, Any]:
    """Generate production Dockerfiles for each service."""
    scan = state.get("repo_scan", {})
    key_files = scan.get("key_files", {})
    scan_dirs = scan.get("dirs", []) if isinstance(scan, dict) else []
    services = state.get("services", [])
    command_hints_state = state.get("command_hints", {}) if isinstance(state.get("command_hints", {}), dict) else {}
    command_map = command_hints_state.get("by_service", {}) if isinstance(command_hints_state, dict) else {}
    
    dockerfiles = {}
    warnings = state.get("docker_generation_warnings", [])
    llm_outputs = state.get("llm_outputs", {})
    if not isinstance(llm_outputs, dict):
        llm_outputs = {}
    if not isinstance(warnings, list):
        warnings = []
    
    for service in services:
        original_ctx = service.get("build_context", ".")
        dockerfile_path = service.get("dockerfile_path", "")
            
        svc_name = service["name"]
        build_ctx = service["build_context"]
        port = service["port"]
        dockerfile_key = dockerfile_path or _get_dockerfile_path(build_ctx)
        available_scripts = _extract_package_scripts(key_files, build_ctx)
        command_hints = command_map.get(svc_name, {}) if isinstance(command_map, dict) else {}
        if not isinstance(command_hints, dict):
            command_hints = {}
        # Look up the pre-existing Dockerfile using the planner-provided path
        existing_dockerfile = None
        if dockerfile_path:
            existing_dockerfile = key_files.get(dockerfile_path)
        
        # ─── Template-first flow ──────────────────────────────────────────
        # 1. Try matching a Supabase template
        # 2. Fall back to deterministic builder
        # 3. Fall back to existing Dockerfile from repo
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
            "service_path": _service_subpath(service) or _normalize_ctx(build_ctx),
            "package_path": _normalize_ctx(state.get("package_path", ".") or "."),
        }
        
        print(f"[docker_gen] Matching template for {svc_name}: tokens={stack_tokens}, signals={template_signals}")
        
        matched = match_template(stack_tokens, template_signals)
        if matched:
            template_vars = dict(matched.get("variables", {}))
            # Override with actual service values
            template_vars["port"] = port
            service_subpath = ""
            if isinstance(dockerfile_path, str) and dockerfile_path.endswith("/Dockerfile"):
                service_subpath = dockerfile_path[: -len("/Dockerfile")]
            template_vars["service_path"] = (
                service_subpath
                if service_subpath
                else (build_ctx if build_ctx != "." else template_vars.get("service_path", "."))
            )
            
            baseline_dockerfile = fill_template(matched["template_content"], template_vars)
            baseline_dockerfile = _inject_command_hints_into_template(
                baseline_dockerfile,
                service=service,
                command_hints=command_hints,
            )
            if str(matched.get("name", "")).strip().lower() == "pnpm_monorepo_vite":
                baseline_dockerfile = _apply_vite_start_fallback(
                    baseline_dockerfile,
                    available_scripts=available_scripts,
                    port=int(port or 5173),
                )
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

        service["build_context"] = original_ctx
        dockerfiles[dockerfile_key] = baseline_dockerfile
    
    state["dockerfiles"] = dockerfiles
    llm_outputs.pop("dockerfile_plan", None)
    state["llm_outputs"] = llm_outputs
    if warnings:
        state["docker_generation_warnings"] = warnings
    return state
