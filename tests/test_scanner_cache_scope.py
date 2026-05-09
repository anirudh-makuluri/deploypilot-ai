import graph.nodes.scanner as scanner_module
from graph.nodes.scanner import _pick_best_cached_response, _hydrate_root_workspace_signals


def test_pick_best_cached_response_root_prefers_full_repo_cache():
    rows = [
        {
            "result": {
                "stack_summary": "FastAPI",
                "_cache_package_path": ".",
            }
        },
        {
            "result": {
                "stack_summary": "Web",
                "_cache_package_path": "apps/web",
            }
        },
    ]

    picked = _pick_best_cached_response(rows, ".")

    assert picked is not None
    assert picked["_cache_package_path"] == "."
    assert picked["stack_summary"] == "FastAPI"


def test_pick_best_cached_response_package_requires_exact_scope():
    rows = [
        {
            "result": {
                "stack_summary": "Monorepo",
                "services": [
                    {"name": "web", "build_context": "apps/web", "port": 3000},
                    {"name": "api", "build_context": "apps/api", "port": 8000},
                ],
                "_cache_package_path": ".",
            }
        }
    ]

    picked = _pick_best_cached_response(rows, "apps/web")

    assert picked is None


def test_pick_best_cached_response_package_uses_exact_package_cache():
    rows = [
        {
            "result": {
                "stack_summary": "Monorepo",
                "_cache_package_path": ".",
            }
        },
        {
            "result": {
                "stack_summary": "Next.js",
                "services": [{"name": "web", "build_context": "apps/web", "port": 3000}],
                "_cache_package_path": "apps/web",
            }
        },
    ]

    picked = _pick_best_cached_response(rows, "apps/web")

    assert picked is not None
    assert picked["_cache_package_path"] == "apps/web"
    assert picked["stack_summary"] == "Next.js"


def test_hydrate_root_workspace_signals_for_scoped_package(monkeypatch):
    scoped_scan = {
        "commit_sha": "abc",
        "key_files": {
            "apps/dashboard/package.json": "{\"name\":\"dashboard\"}",
        },
        "dirs": ["apps/dashboard", "apps/dashboard/src"],
    }

    class _FakeFetch:
        def invoke(self, _payload):
            return {
                "commit_sha": "abc",
                "key_files": {
                    "package.json": "{\"workspaces\":[\"apps/*\"]}",
                    "pnpm-lock.yaml": "lockfileVersion: '9.0'",
                    "pnpm-workspace.yaml": "packages:\\n  - apps/*\\n",
                    "turbo.json": "{\"pipeline\":{}}",
                },
                "dirs": ["apps", "packages", "apps/dashboard"],
            }

    monkeypatch.setattr(scanner_module, "fetch_repo_structure", _FakeFetch())

    hydrated = _hydrate_root_workspace_signals(
        scoped_scan,
        repo_url="https://github.com/acme/repo",
        github_token=None,
        max_files=50,
        package_path="apps/dashboard",
    )

    assert "package.json" in hydrated["key_files"]
    assert "pnpm-lock.yaml" in hydrated["key_files"]
    assert "pnpm-workspace.yaml" in hydrated["key_files"]
    assert "turbo.json" in hydrated["key_files"]
    assert "packages" in hydrated["dirs"]
