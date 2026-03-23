from graph.graph import check_compose_required, check_planner_error, check_scanner_error


def test_check_scanner_error_routes_on_error():
    assert check_scanner_error({"error": "scan failed"}) == "error_or_cached"


def test_check_scanner_error_routes_on_cached_response():
    assert check_scanner_error({"cached_response": {"ok": True}}) == "error_or_cached"


def test_check_scanner_error_continues_without_error_or_cache():
    assert check_scanner_error({}) == "continue"


def test_check_planner_error_routes_on_error():
    assert check_planner_error({"error": "not deployable"}) == "error"


def test_check_planner_error_continues_without_error():
    assert check_planner_error({"services": []}) == "continue"


def test_check_compose_required_for_multi_service_repo():
    state = {
        "services": [
            {"name": "web", "build_context": ".", "port": 3000},
            {"name": "api", "build_context": "./api", "port": 8000},
        ]
    }
    assert check_compose_required(state) == "compose"


def test_check_compose_required_skips_for_single_service_repo():
    state = {
        "services": [
            {"name": "web", "build_context": ".", "port": 3000},
        ]
    }
    assert check_compose_required(state) == "skip"


def test_check_compose_required_skips_for_scoped_monorepo():
    state = {
        "package_path": "apps/web",
        "services": [
            {"name": "web", "build_context": "apps/web", "port": 3000},
            {"name": "web-worker", "build_context": "apps/web", "port": 3001},
        ]
    }
    assert check_compose_required(state) == "skip"


def test_check_compose_required_skips_when_services_missing():
    assert check_compose_required({}) == "skip"
