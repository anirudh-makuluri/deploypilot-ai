from graph.nodes.scanner import _pick_best_cached_response


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
