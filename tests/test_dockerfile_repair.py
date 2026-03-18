from graph.nodes.dockerfile_generator import _repair_dockerfile_output


def test_repair_adds_wget_to_runner_when_healthcheck_uses_wget():
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

    assert "RUN apk add --no-cache wget" in repaired


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
