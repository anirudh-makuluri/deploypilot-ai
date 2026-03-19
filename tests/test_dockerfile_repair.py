from graph.nodes.dockerfile_generator import _repair_dockerfile_output


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
