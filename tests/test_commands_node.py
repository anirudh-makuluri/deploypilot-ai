from graph.nodes.commands_generator import commands_generator_node


def test_commands_node_single_service_generates_docker_commands():
    state = {
        "services": [
            {"name": "api", "build_context": ".", "port": 8000},
        ],
        "stack_tokens": ["python", "fastapi"],
        "repo_scan": {"key_files": {}},
    }

    out = commands_generator_node(state)

    assert "commands" in out
    assert "." in out["commands"]
    root_cmds = out["commands"]["."]
    assert any(cmd.startswith("docker build") for cmd in root_cmds)
    assert not any("docker compose" in cmd for cmd in root_cmds)
    assert "command_hints" in out
    assert "api" in out["command_hints"]["by_service"]


def test_commands_node_multi_service_generates_compose_commands():
    state = {
        "services": [
            {"name": "web", "build_context": ".", "port": 3000},
            {"name": "api", "build_context": "api", "port": 8000},
        ],
        "stack_tokens": ["node", "react"],
        "repo_scan": {"key_files": {}},
    }

    out = commands_generator_node(state)

    assert "." in out["commands"]
    root_cmds = out["commands"]["."]
    assert any(cmd.startswith("docker compose build") for cmd in root_cmds)
    assert any(cmd.startswith("docker compose up") for cmd in root_cmds)


def test_commands_node_prefers_package_scripts_when_available():
    state = {
        "services": [
            {"name": "web", "build_context": ".", "port": 3000},
        ],
        "stack_tokens": ["node", "pnpm"],
        "repo_scan": {
            "key_files": {
                "package.json": '{"scripts":{"build":"next build","start":"next start"}}',
                "pnpm-lock.yaml": "lockfileVersion: '9.0'",
            }
        },
    }

    out = commands_generator_node(state)

    svc_cmds = out["command_hints"]["by_service"]["web"]
    assert svc_cmds["install"] == "pnpm install --frozen-lockfile"
    assert svc_cmds["build"] == "pnpm build"
    assert svc_cmds["run"] == "pnpm start"


def test_commands_node_emits_docker_build_contract():
    state = {
        "services": [
            {
                "name": "web",
                "build_context": ".",
                "dockerfile_path": "apps/web/Dockerfile",
                "execution_root": ".",
                "port": 3000,
            },
        ],
        "stack_tokens": ["node", "vite"],
        "repo_scan": {"key_files": {}},
    }

    out = commands_generator_node(state)
    svc_cmds = out["command_hints"]["by_service"]["web"]
    assert svc_cmds["docker_build"] == "docker build -f apps/web/Dockerfile ."
    assert svc_cmds["execution_root"] == "."


def test_commands_node_compose_execution_root_follows_package_path():
    state = {
        "package_path": "apps/web",
        "services": [
            {"name": "web", "build_context": "apps/web", "port": 3000},
            {"name": "api", "build_context": "apps/api", "port": 8000},
        ],
        "stack_tokens": ["node"],
        "repo_scan": {"key_files": {}},
    }

    out = commands_generator_node(state)
    assert "apps/web" in out["commands"]
    compose_cmds = out["commands"]["apps/web"]
    assert any(cmd.startswith("docker compose build") for cmd in compose_cmds)
