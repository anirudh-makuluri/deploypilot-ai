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
    assert "by_service" in out["commands"]
    assert "api" in out["commands"]["by_service"]
    assert any(cmd.startswith("docker build") for cmd in out["commands"].get("global", []))
    assert not any("docker compose" in cmd for cmd in out["commands"].get("global", []))


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

    global_commands = out["commands"].get("global", [])
    assert any(cmd.startswith("docker compose build") for cmd in global_commands)
    assert any(cmd.startswith("docker compose up") for cmd in global_commands)


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

    svc_cmds = out["commands"]["by_service"]["web"]
    assert svc_cmds["install"] == "pnpm install --frozen-lockfile"
    assert svc_cmds["build"] == "pnpm build"
    assert svc_cmds["run"] == "pnpm start"
