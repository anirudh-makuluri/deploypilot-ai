import json

from graph.nodes.planner import planner_node


class _Resp:
    def __init__(self, content: str):
        self.content = content


class _FakePlannerLLM:
    def __init__(self, invoke_fn):
        self._invoke_fn = invoke_fn

    def invoke(self, prompt: str):
        return self._invoke_fn(prompt)


def test_planner_retries_and_recovers(monkeypatch):
    calls = {"count": 0}

    def _invoke(_prompt: str):
        calls["count"] += 1
        if calls["count"] == 1:
            return _Resp("{not-json")
        payload = {
            "is_deployable": True,
            "error_reason": "",
            "stack_tokens": ["python", "fastapi"],
            "services": [
                {
                    "name": "api",
                    "build_context": ".",
                    "port": 8000,
                    "dockerfile_path": "",
                }
            ],
            "has_existing_dockerfiles": False,
            "has_existing_compose": False,
        }
        return _Resp(json.dumps(payload))

    monkeypatch.setattr("graph.nodes.planner.llm_planner", _FakePlannerLLM(_invoke))

    state = {
        "repo_scan": {
            "key_files": {},
            "dirs": [],
        }
    }

    out = planner_node(state)

    assert "error" not in out
    assert out["stack_tokens"] == ["python", "fastapi"]
    assert out["detected_stack"] == "Python, FastAPI"
    assert out["planner_retry_attempts"] == 2
    assert out["planner_fallback_used"] is False


def test_planner_returns_error_after_retry_exhaustion(monkeypatch):
    def _invoke(_prompt: str):
        return _Resp("{still-bad-json")

    monkeypatch.setattr("graph.nodes.planner.llm_planner", _FakePlannerLLM(_invoke))

    state = {
        "repo_scan": {
            "key_files": {},
            "dirs": [],
        }
    }

    out = planner_node(state)

    assert "error" in out
    assert "Failed to analyze repository" in out["error"]


def test_planner_uses_deterministic_fallback_when_llm_services_empty(monkeypatch):
    def _invoke(_prompt: str):
        payload = {
            "is_deployable": True,
            "error_reason": "",
            "stack_tokens": ["next", "node"],
            "services": [],
            "has_existing_dockerfiles": False,
            "has_existing_compose": False,
        }
        return _Resp(json.dumps(payload))

    monkeypatch.setattr("graph.nodes.planner.llm_planner", _FakePlannerLLM(_invoke))
    monkeypatch.setattr(
        "graph.nodes.planner.extract_port_and_stack",
        lambda *args, **kwargs: {
            "success": True,
            "port": 3000,
            "port_confidence": 0.9,
            "stack_tokens": ["node", "next", "react"],
        },
    )

    state = {
        "repo_url": "https://github.com/example/repo",
        "package_path": "apps/web",
        "repo_scan": {
            "key_files": {},
            "dirs": [],
        },
    }

    out = planner_node(state)

    assert "error" not in out
    assert out.get("planner_used_deterministic_fallback") is True
    assert len(out["services"]) == 1
    assert out["services"][0]["build_context"] == "apps/web"
    assert out["services"][0]["port"] == 3000


def test_planner_uses_deterministic_fallback_when_llm_fails(monkeypatch):
    def _invoke(_prompt: str):
        return _Resp("{not-json")

    monkeypatch.setattr("graph.nodes.planner.llm_planner", _FakePlannerLLM(_invoke))
    monkeypatch.setattr(
        "graph.nodes.planner.extract_port_and_stack",
        lambda *args, **kwargs: {
            "success": True,
            "port": 8000,
            "port_confidence": 0.9,
            "stack_tokens": ["python", "fastapi"],
        },
    )

    state = {
        "repo_url": "https://github.com/example/repo",
        "package_path": "backend",
        "repo_scan": {
            "key_files": {
                "backend/requirements.txt": "fastapi==0.110.0\nuvicorn==0.29.0",
            },
            "dirs": ["backend"],
        },
    }

    out = planner_node(state)

    assert "error" not in out
    assert out.get("planner_used_deterministic_fallback") is True
    assert out["services"][0]["name"] == "api"
    assert out["services"][0]["build_context"] == "backend"
    assert out["services"][0]["port"] == 8000


def test_planner_deterministic_fallback_prefers_nested_next_app_context(monkeypatch):
    def _invoke(_prompt: str):
        payload = {
            "is_deployable": True,
            "error_reason": "",
            "stack_tokens": ["next", "node"],
            "services": [],
            "has_existing_dockerfiles": False,
            "has_existing_compose": True,
        }
        return _Resp(json.dumps(payload))

    monkeypatch.setattr("graph.nodes.planner.llm_planner", _FakePlannerLLM(_invoke))
    monkeypatch.setattr(
        "graph.nodes.planner.extract_port_and_stack",
        lambda *args, **kwargs: {
            "success": True,
            "port": 3000,
            "port_confidence": 0.9,
            "stack_tokens": ["node", "next", "react"],
        },
    )

    state = {
        "repo_url": "https://github.com/vercel/next.js",
        "package_path": "examples/with-docker-compose",
        "repo_scan": {
            "key_files": {
                "examples/with-docker-compose/docker-compose.yml": "services:\n  app:\n    build: ./next-app",
            },
            "dirs": [
                "examples/with-docker-compose/next-app",
                "examples/with-docker-compose/db",
            ],
        },
    }

    out = planner_node(state)

    assert "error" not in out
    assert out.get("planner_used_deterministic_fallback") is True
    assert out["services"][0]["build_context"] == "examples/with-docker-compose/next-app"
    assert out["services"][0]["name"] == "web"


def test_planner_deterministic_fallback_keeps_parent_context_for_generic_app_dir(monkeypatch):
    def _invoke(_prompt: str):
        payload = {
            "is_deployable": True,
            "error_reason": "",
            "stack_tokens": ["fastapi", "python"],
            "services": [],
            "has_existing_dockerfiles": False,
            "has_existing_compose": False,
        }
        return _Resp(json.dumps(payload))

    monkeypatch.setattr("graph.nodes.planner.llm_planner", _FakePlannerLLM(_invoke))
    monkeypatch.setattr(
        "graph.nodes.planner.extract_port_and_stack",
        lambda *args, **kwargs: {
            "success": True,
            "port": 8000,
            "port_confidence": 0.9,
            "stack_tokens": ["python", "fastapi", "uvicorn"],
        },
    )

    state = {
        "repo_url": "https://github.com/tiangolo/full-stack-fastapi-template",
        "package_path": "backend",
        "repo_scan": {
            "key_files": {},
            "dirs": [
                "backend/app",
            ],
        },
    }

    out = planner_node(state)

    assert "error" not in out
    assert out.get("planner_used_deterministic_fallback") is True
    assert out["services"][0]["build_context"] == "backend"
