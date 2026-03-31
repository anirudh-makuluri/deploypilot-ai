from graph.nodes.planner import ServiceInfo, _filter_services_by_selector


def test_filter_services_by_selector_matches_name():
    services = [
        ServiceInfo(name="web", build_context=".", port=3000, dockerfile_path="Dockerfile"),
        ServiceInfo(name="websocket", build_context=".", port=3001, dockerfile_path="Dockerfile.websocket"),
    ]
    selected, match_kind = _filter_services_by_selector(services, "web")
    assert match_kind == "name"
    assert len(selected) == 1
    assert selected[0].name == "web"


def test_filter_services_by_selector_matches_dockerfile_path():
    services = [
        ServiceInfo(name="web", build_context=".", port=3000, dockerfile_path="Dockerfile"),
        ServiceInfo(name="websocket", build_context=".", port=3001, dockerfile_path="Dockerfile.websocket"),
    ]
    selected, match_kind = _filter_services_by_selector(services, "Dockerfile.websocket")
    assert match_kind == "dockerfile_path"
    assert len(selected) == 1
    assert selected[0].name == "websocket"
