import copy

import pytest

from graph.feedback import (
    CoordinatorOutput,
    ChangeInstruction,
    run_feedback_improvement,
)
from graph.nodes.verifier import VerifierOutput


class _Resp:
    def __init__(self, content: str):
        self.content = content


class _FakeInvokeLLM:
    def __init__(self, invoke_fn):
        self._invoke_fn = invoke_fn

    def invoke(self, prompt: str):
        return self._invoke_fn(prompt)


class _FakeStructuredLLM:
    def __init__(self, invoke_fn):
        self._invoke_fn = invoke_fn

    def invoke(self, prompt: str):
        return self._invoke_fn(prompt)


class _FakeLLMWithStructured:
    def __init__(self, invoke_fn=None, structured_invoke_fn=None):
        self._invoke_fn = invoke_fn or (lambda _prompt: _Resp(""))
        self._structured_invoke_fn = structured_invoke_fn or (lambda _prompt: None)

    def invoke(self, prompt: str):
        return self._invoke_fn(prompt)

    def with_structured_output(self, _schema):
        return _FakeStructuredLLM(self._structured_invoke_fn)


@pytest.fixture
def base_cached_result():
    return {
        "commit_sha": "abc123",
        "stack_summary": "Python API",
        "services": [{"name": "api", "build_context": ".", "port": 8000}],
        "dockerfiles": {"Dockerfile": "FROM python:3.11-slim\nEXPOSE 8000\n"},
        "docker_compose": "services:\n  api:\n    build: .\n    ports:\n      - \"8000:8000\"\n",
        "nginx_conf": "events {}\nhttp { server { listen 80; } }\n",
        "has_existing_dockerfiles": False,
        "has_existing_compose": False,
        "risks": ["Missing HEALTHCHECK"],
        "hadolint_results": {"Dockerfile": "DL3008 warning"},
    }


def _patch_common(monkeypatch, *, coordinator_plan, docker_out, compose_out, nginx_out):
    monkeypatch.setattr(
        "graph.feedback.llm_coordinator",
        _FakeLLMWithStructured(structured_invoke_fn=lambda _p: CoordinatorOutput(change_plan=coordinator_plan, summary="ok")),
    )
    monkeypatch.setattr(
        "graph.feedback.llm_docker",
        _FakeInvokeLLM(lambda _p: _Resp(docker_out)),
    )
    monkeypatch.setattr(
        "graph.feedback.llm_compose",
        _FakeInvokeLLM(lambda _p: _Resp(compose_out)),
    )
    monkeypatch.setattr(
        "graph.feedback.llm_nginx",
        _FakeInvokeLLM(lambda _p: _Resp(nginx_out)),
    )
    monkeypatch.setattr(
        "graph.feedback.llm_verifier",
        _FakeLLMWithStructured(structured_invoke_fn=lambda _p: VerifierOutput(confidence=0.92, risks=[])),
    )
    monkeypatch.setattr("graph.feedback.run_hadolint", lambda _content: "")


def test_coordinator_targets_only_nginx(monkeypatch, base_cached_result):
    plan = [
        ChangeInstruction(artifact_type="dockerfile", service_name="api", should_change=False, instructions=""),
        ChangeInstruction(artifact_type="compose", service_name="", should_change=False, instructions=""),
        ChangeInstruction(artifact_type="nginx", service_name="", should_change=True, instructions="Update route"),
    ]
    _patch_common(
        monkeypatch,
        coordinator_plan=plan,
        docker_out="FROM python:3.12-slim\n",
        compose_out="services:\n  changed: true\n",
        nginx_out="events {}\nhttp { server { location /api { proxy_pass http://localhost:8000; } } }\n",
    )

    out = run_feedback_improvement(base_cached_result, "fix nginx path")

    assert out["dockerfiles"] == base_cached_result["dockerfiles"]
    assert out["docker_compose"] == base_cached_result["docker_compose"]
    assert out["nginx_conf"] != base_cached_result["nginx_conf"]


def test_coordinator_targets_only_dockerfile(monkeypatch, base_cached_result):
    plan = [
        ChangeInstruction(artifact_type="dockerfile", service_name="Dockerfile", should_change=True, instructions="Add HEALTHCHECK"),
        ChangeInstruction(artifact_type="compose", service_name="", should_change=False, instructions=""),
        ChangeInstruction(artifact_type="nginx", service_name="", should_change=False, instructions=""),
    ]
    _patch_common(
        monkeypatch,
        coordinator_plan=plan,
        docker_out="FROM python:3.11-slim\nHEALTHCHECK CMD curl -f http://localhost:8000/health || exit 1\n",
        compose_out="services:\n  changed: true\n",
        nginx_out="events {}\nhttp { server { location /new { return 200; } } }\n",
    )

    out = run_feedback_improvement(base_cached_result, "add healthcheck")

    assert out["dockerfiles"]["Dockerfile"] != base_cached_result["dockerfiles"]["Dockerfile"]
    assert out["docker_compose"] == base_cached_result["docker_compose"]
    assert out["nginx_conf"] == base_cached_result["nginx_conf"]


def test_coordinator_targets_all(monkeypatch, base_cached_result):
    plan = [
        ChangeInstruction(artifact_type="dockerfile", service_name="Dockerfile", should_change=True, instructions="Change base image"),
        ChangeInstruction(artifact_type="compose", service_name="", should_change=True, instructions="Add env"),
        ChangeInstruction(artifact_type="nginx", service_name="", should_change=True, instructions="Add /api route"),
    ]
    _patch_common(
        monkeypatch,
        coordinator_plan=plan,
        docker_out="FROM python:3.12-slim\n",
        compose_out="services:\n  api:\n    environment:\n      - A=1\n",
        nginx_out="events {}\nhttp { server { location /api { proxy_pass http://localhost:8000; } } }\n",
    )

    out = run_feedback_improvement(base_cached_result, "do all changes")

    assert out["dockerfiles"]["Dockerfile"] != base_cached_result["dockerfiles"]["Dockerfile"]
    assert out["docker_compose"] != base_cached_result["docker_compose"]
    assert out["nginx_conf"] != base_cached_result["nginx_conf"]


def test_coordinator_fallback_on_llm_failure(monkeypatch, base_cached_result):
    monkeypatch.setattr(
        "graph.feedback.llm_coordinator",
        _FakeLLMWithStructured(structured_invoke_fn=lambda _p: (_ for _ in ()).throw(RuntimeError("coordinator down"))),
    )
    monkeypatch.setattr("graph.feedback.llm_docker", _FakeInvokeLLM(lambda _p: _Resp("FROM python:3.12-slim\n")))
    monkeypatch.setattr("graph.feedback.llm_compose", _FakeInvokeLLM(lambda _p: _Resp("services:\n  api:\n    build: .\n")))
    monkeypatch.setattr("graph.feedback.llm_nginx", _FakeInvokeLLM(lambda _p: _Resp("events {}\nhttp { server { listen 80; } }\n")))
    monkeypatch.setattr(
        "graph.feedback.llm_verifier",
        _FakeLLMWithStructured(structured_invoke_fn=lambda _p: VerifierOutput(confidence=0.8, risks=[])),
    )
    monkeypatch.setattr("graph.feedback.run_hadolint", lambda _content: "")

    out = run_feedback_improvement(base_cached_result, "fix deploy")

    assert "dockerfiles" in out
    assert "docker_compose" in out
    assert "nginx_conf" in out


def test_dockerfile_improver_skips_unchanged_service(monkeypatch, base_cached_result):
    cached = copy.deepcopy(base_cached_result)
    cached["services"] = [
        {"name": "api", "build_context": ".", "port": 8000},
        {"name": "worker", "build_context": ".", "port": 9000},
    ]
    cached["dockerfiles"]["worker"] = "FROM python:3.11-slim\nCMD ['python', 'worker.py']\n"

    plan = [
        ChangeInstruction(artifact_type="dockerfile", service_name="api", should_change=True, instructions="update"),
        ChangeInstruction(artifact_type="dockerfile", service_name="worker", should_change=False, instructions=""),
        ChangeInstruction(artifact_type="compose", service_name="", should_change=False, instructions=""),
        ChangeInstruction(artifact_type="nginx", service_name="", should_change=False, instructions=""),
    ]

    _patch_common(
        monkeypatch,
        coordinator_plan=plan,
        docker_out="FROM python:3.12-slim\n",
        compose_out="services:\n  changed: true\n",
        nginx_out="events {}\nhttp { server { return 200; } }\n",
    )

    out = run_feedback_improvement(cached, "update only api dockerfile")

    assert out["dockerfiles"]["worker"] == cached["dockerfiles"]["worker"]
    assert out["dockerfiles"]["api"] != cached["dockerfiles"]["api"]


def test_verifier_runs_and_writes_confidence(monkeypatch, base_cached_result):
    plan = [
        ChangeInstruction(artifact_type="dockerfile", service_name="api", should_change=False, instructions=""),
        ChangeInstruction(artifact_type="compose", service_name="", should_change=False, instructions=""),
        ChangeInstruction(artifact_type="nginx", service_name="", should_change=False, instructions=""),
    ]

    _patch_common(
        monkeypatch,
        coordinator_plan=plan,
        docker_out="FROM python:3.12-slim\n",
        compose_out="services:\n  changed: true\n",
        nginx_out="events {}\nhttp { server { return 200; } }\n",
    )

    out = run_feedback_improvement(base_cached_result, "no-op")

    assert out["confidence"] == 0.92
    assert out["risks"] == []


def test_run_feedback_improvement_public_api_shape(monkeypatch, base_cached_result):
    plan = [
        ChangeInstruction(artifact_type="dockerfile", service_name="api", should_change=True, instructions="update"),
        ChangeInstruction(artifact_type="compose", service_name="", should_change=True, instructions="update"),
        ChangeInstruction(artifact_type="nginx", service_name="", should_change=True, instructions="update"),
    ]

    _patch_common(
        monkeypatch,
        coordinator_plan=plan,
        docker_out="FROM python:3.12-slim\n",
        compose_out="services:\n  api:\n    build: .\n",
        nginx_out="events {}\nhttp { server { listen 80; } }\n",
    )

    out = run_feedback_improvement(base_cached_result, "shape check")

    expected_keys = {
        "commit_sha",
        "stack_summary",
        "services",
        "dockerfiles",
        "docker_compose",
        "nginx_conf",
        "has_existing_dockerfiles",
        "has_existing_compose",
        "risks",
        "confidence",
        "hadolint_results",
    }
    assert expected_keys.issubset(set(out.keys()))
