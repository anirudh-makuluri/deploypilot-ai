from graph.nodes.dockerfile_generator import _repair_dockerfile_output
from graph.nodes.dockerfile_generator import dockerfile_generator_node


def test_repair_removes_healthcheck_instructions():
    content = """FROM node:20-alpine AS base
FROM base AS deps
WORKDIR /app
COPY apps/backend/package.json pnpm-lock.yaml* ./
RUN corepack enable pnpm && pnpm i --frozen-lockfile
FROM base AS runner
WORKDIR /app
HEALTHCHECK CMD wget -qO- http://localhost:5000 || exit 1
"""

    repaired = _repair_dockerfile_output(
        content=content,
        service={"name": "backend", "build_context": "apps/backend"},
        key_files={"pnpm-lock.yaml": "lock", "package.json": "{}"},
        available_scripts=["build"],
    )

    assert "HEALTHCHECK" not in repaired


def test_repair_monorepo_install_uses_workspace_context_and_filter():
    content = """FROM node:20-alpine AS base
FROM base AS deps
WORKDIR /app
COPY apps/web/package.json pnpm-lock.yaml* ./
RUN corepack enable pnpm && pnpm i --frozen-lockfile
"""

    repaired = _repair_dockerfile_output(
        content=content,
        service={"name": "web", "build_context": "apps/web"},
        key_files={
            "package.json": "{}",
            "pnpm-lock.yaml": "lock",
            "pnpm-workspace.yaml": "packages:\n  - apps/*\n",
            "packages/shared/package.json": "{}",
        },
        available_scripts=["build"],
    )

    assert "COPY package.json ./" in repaired
    assert "COPY pnpm-lock.yaml* ./" in repaired
    assert "COPY pnpm-workspace.yaml* ./" in repaired
    assert "COPY apps/web/package.json apps/web/package.json" in repaired
    assert "COPY packages ./packages" in repaired
    assert "pnpm i --frozen-lockfile --filter ./apps/web..." in repaired


def test_repair_normalizes_shell_form_node_cmd_to_json_exec_form():
    content = """FROM node:20-alpine AS runner
CMD node server.js
"""

    repaired = _repair_dockerfile_output(
        content=content,
        service={"name": "web", "build_context": "apps/web"},
        key_files={},
        available_scripts=["build"],
    )

    assert 'CMD ["node", "server.js"]' in repaired


def test_repair_prefers_pnpm_start_for_backend_when_start_script_exists():
    content = """FROM node:20-alpine AS runner
CMD ["node", "app.js"]
"""

    repaired = _repair_dockerfile_output(
        content=content,
        service={"name": "backend", "build_context": "apps/backend"},
        key_files={},
        available_scripts=["start", "build"],
    )

    assert 'CMD ["pnpm", "start"]' in repaired


def test_repair_copies_packages_when_packages_dir_marker_present():
    content = """FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json ./
COPY pnpm-lock.yaml* ./
COPY pnpm-workspace.yaml* ./
COPY apps/backend/package.json apps/backend/package.json
RUN corepack enable pnpm && pnpm i --frozen-lockfile --filter ./apps/backend...
"""

    repaired = _repair_dockerfile_output(
        content=content,
        service={"name": "backend", "build_context": "apps/backend"},
        key_files={
            "pnpm-lock.yaml": "lock",
            "pnpm-workspace.yaml": "workspace",
            "package.json": "{}",
            "__has_packages_dir__": True,
        },
        available_scripts=["start"],
    )

    assert "COPY packages ./packages" in repaired


def test_repair_fixes_duplicate_filter_and_nested_node_modules_copy_and_build_step():
    content = """FROM node:20-alpine AS base
FROM base AS deps
WORKDIR /app
COPY pnpm-lock.yaml pnpm-workspace.yaml package.json ./
COPY apps/backend/package.json ./apps/backend/package.json
RUN corepack enable pnpm && pnpm i --frozen-lockfile --filter ./apps/backend... --filter ./apps/backend...
FROM base AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
WORKDIR /app/apps/backend
FROM base AS runner
WORKDIR /app
COPY --from=builder /app/apps/backend/node_modules ./node_modules
COPY --from=builder /app/apps/backend .
"""

    repaired = _repair_dockerfile_output(
        content=content,
        service={"name": "backend", "build_context": "apps/backend"},
        key_files={
            "pnpm-lock.yaml": "lock",
            "pnpm-workspace.yaml": "packages:\n  - apps/*\n",
            "package.json": "{}",
            "apps/backend/package.json": '{"scripts": {"build": "echo build"}}',
            "packages/shared/package.json": "{}",
        },
        available_scripts=["build"],
    )

    assert repaired.count("--filter ./apps/backend...") == 1
    assert "COPY --from=builder /app/node_modules ./node_modules" in repaired
    assert "RUN if [ -f package.json ] && grep -q '\"build\"' package.json; then" in repaired
    assert "\nFROM base AS builder\n" in repaired


def test_generator_keeps_root_context_for_nested_dockerfile_and_emits_workspace_safe_commands():
    state = {
        "services": [
            {
                "name": "dashboard",
                "build_context": ".",
                "dockerfile_path": "apps/dashboard/Dockerfile",
                "port": 5173,
            }
        ],
        "stack_tokens": ["node", "vite", "pnpm"],
        "repo_scan": {
            "key_files": {
                "package.json": "{}",
                "pnpm-lock.yaml": "lock",
                "pnpm-workspace.yaml": "packages:\n  - apps/*\n",
                "apps/dashboard/package.json": '{"scripts":{"build":"vite build","start":"vite preview --host 0.0.0.0 --port 5173"}}',
            }
        },
        "command_hints": {
            "by_service": {
                "dashboard": {
                    "install": "pnpm install --frozen-lockfile",
                    "build": "pnpm build",
                    "run": "pnpm start",
                }
            }
        },
    }

    out = dockerfile_generator_node(state)
    dockerfile = out["dockerfiles"]["apps/dashboard/Dockerfile"]

    assert "COPY pnpm-lock.yaml* ./" in dockerfile
    assert "COPY pnpm-workspace.yaml* ./" in dockerfile
    assert "COPY apps/dashboard/package.json apps/dashboard/package.json" in dockerfile
    assert "pnpm i --frozen-lockfile --filter ./apps/dashboard..." in dockerfile
    assert "--filter ./...." not in dockerfile
    assert "RUN pnpm build" in dockerfile or "RUN pnpm --filter ./apps/dashboard... build" in dockerfile
    assert "\nFROM base AS runner\nWORKDIR /app/apps/dashboard\n" in dockerfile
    assert 'CMD ["pnpm", "start"]' in dockerfile


def test_template_command_injection_does_not_downgrade_pnpm_to_npm_or_dev():
    state = {
        "services": [
            {
                "name": "dashboard",
                "build_context": ".",
                "dockerfile_path": "apps/dashboard/Dockerfile",
                "port": 5173,
            }
        ],
        "package_path": "apps/dashboard",
        "stack_tokens": ["node", "react", "vite", "pnpm"],
        "repo_scan": {
            "key_files": {
                "package.json": "{}",
                "pnpm-lock.yaml": "lock",
                "pnpm-workspace.yaml": "packages:\n  - apps/*\n",
                "apps/dashboard/package.json": '{"scripts":{"build":"vite build","start":"vite preview --host 0.0.0.0 --port 5173","dev":"vite"}}',
            }
        },
        # Simulate noisy/non-production hints from command generation.
        "command_hints": {
            "by_service": {
                "dashboard": {
                    "install": "npm ci",
                    "build": "npm run build",
                    "run": "npm run dev",
                }
            }
        },
    }

    out = dockerfile_generator_node(state)
    dockerfile = out["dockerfiles"]["apps/dashboard/Dockerfile"]

    assert "RUN corepack enable pnpm && pnpm i --frozen-lockfile --filter ./apps/dashboard..." in dockerfile
    assert dockerfile.count("--filter ./apps/dashboard...") == 1
    assert "npm ci" not in dockerfile
    assert "--filter ./...." not in dockerfile
    assert 'CMD ["npm", "run", "dev"]' not in dockerfile
    assert "WORKDIR /app/apps/dashboard" in dockerfile
    assert 'CMD ["pnpm", "start"]' in dockerfile
    # Vite monorepo template should drive filtered build command.
    assert "RUN pnpm --filter ./apps/dashboard... build" in dockerfile


def test_vite_template_falls_back_to_preview_when_start_script_missing():
    state = {
        "services": [
            {
                "name": "dashboard",
                "build_context": ".",
                "dockerfile_path": "apps/dashboard/Dockerfile",
                "port": 5173,
            }
        ],
        "package_path": "apps/dashboard",
        "stack_tokens": ["node", "react", "vite", "pnpm"],
        "repo_scan": {
            "key_files": {
                "package.json": "{}",
                "pnpm-lock.yaml": "lock",
                "pnpm-workspace.yaml": "packages:\n  - apps/*\n",
                "apps/dashboard/package.json": '{"scripts":{"build":"vite build","preview":"vite preview"}}',
            }
        },
        "command_hints": {
            "by_service": {
                "dashboard": {
                    "install": "pnpm install --frozen-lockfile",
                    "build": "pnpm run build",
                    "run": "pnpm start",
                }
            }
        },
    }

    out = dockerfile_generator_node(state)
    dockerfile = out["dockerfiles"]["apps/dashboard/Dockerfile"]

    assert 'CMD ["pnpm", "start"]' not in dockerfile
    assert 'CMD ["pnpm", "preview", "--host", "0.0.0.0", "--port", "5173"]' in dockerfile
