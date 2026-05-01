from graph.nodes.preflight import preflight_node


def test_preflight_flags_copy_outside_context():
    state = {
        "services": [{"name": "web", "build_context": "apps/web", "dockerfile_path": "apps/web/Dockerfile"}],
        "dockerfiles": {"apps/web/Dockerfile": "FROM node:20\nCOPY ../package.json ./\n"},
        "repo_scan": {"key_files": {"apps/web/package.json": "{}"}, "dirs": ["apps/web"]},
    }
    out = preflight_node(state)
    assert out["preflight_issues"]
    assert "escapes build context" in out["preflight_issues"][0]
    assert out["error"]["code"] == "preflight_failed"


def test_preflight_accepts_valid_copy_in_context():
    state = {
        "services": [{"name": "web", "build_context": "apps/web", "dockerfile_path": "apps/web/Dockerfile"}],
        "dockerfiles": {"apps/web/Dockerfile": "FROM node:20\nCOPY package.json ./\n"},
        "repo_scan": {"key_files": {"apps/web/package.json": "{}"}, "dirs": ["apps/web"]},
    }
    out = preflight_node(state)
    assert out["preflight_issues"] == []
    assert "error" not in out
