import json
import time

import tools.evaluate_scan_quality as evaluate_scan_quality

from tools.evaluate_scan_quality import (
    _build_artifact_summary,
    _build_generated_artifact_scores,
    _build_repo_report,
    _compose_generation_audit,
    _evaluate_targets,
    _failure_bucket_from_report,
    _load_labels,
    _select_compose_file,
    _select_dockerfile,
    _select_nginx_file,
    _summarize_compose_generation_audits,
)


def test_load_labels_prefers_repo_url(tmp_path):
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(
        json.dumps(
            {
                "repos": [
                    {
                        "repo_url": "https://github.com/vercel/next.js",
                        "package_path": "examples/with-docker",
                        "expected_services": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    labels = _load_labels(str(labels_path))

    assert len(labels) == 1
    assert labels[0]["repo"] == "vercel/next.js"
    assert labels[0]["repo_url"] == "https://github.com/vercel/next.js"
    assert labels[0]["package_path"] == "examples/with-docker"


def test_load_labels_builds_repo_url_from_repo_name(tmp_path):
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(
        json.dumps(
            {
                "repos": [
                    {
                        "repo": "tiangolo/full-stack-fastapi-template",
                        "expected_services": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    labels = _load_labels(str(labels_path))

    assert len(labels) == 1
    assert labels[0]["repo"] == "tiangolo/full-stack-fastapi-template"
    assert labels[0]["repo_url"] == "https://github.com/tiangolo/full-stack-fastapi-template"
    assert labels[0]["package_path"] == "."
    assert labels[0]["artifact_expectations"] == {}
    assert labels[0]["artifact_scoring_overrides"] == {}


def test_load_labels_accepts_artifact_expectation_fields(tmp_path):
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(
        json.dumps(
            {
                "repos": [
                    {
                        "repo": "vercel/next.js",
                        "artifact_expectations": {
                            "dockerfile": {"required_instructions": ["HEALTHCHECK"]},
                            "compose": {"required_services": ["web"]},
                        },
                        "artifact_scoring_overrides": {
                            "dockerfile": {"healthcheck": 0.30},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    labels = _load_labels(str(labels_path))

    assert len(labels) == 1
    assert labels[0]["artifact_expectations"]["dockerfile"]["required_instructions"] == ["HEALTHCHECK"]
    assert labels[0]["artifact_scoring_overrides"]["dockerfile"]["healthcheck"] == 0.30


def test_failure_bucket_classifies_context_errors():
    report = {
        "error": "No repository context provided to analyze",
        "metrics": {},
    }

    assert _failure_bucket_from_report(report) == "planner_context_missing"


def test_failure_bucket_classifies_port_missing():
    report = {
        "error": None,
        "metrics": {
            "known_port_count": 1,
            "correct_port_count": 0,
            "missing_port_count": 1,
            "false_negatives": 0,
            "true_positives": 1,
            "false_positives": 0,
            "stack_match": True,
        },
    }

    assert _failure_bucket_from_report(report) == "port_missing"


def test_select_compose_file_prefers_package_local_file():
    key_files = {
        "docker-compose.yml": "services: { root: {} }",
        "apps/web/docker-compose.yml": "services: { web: {} }",
    }

    selected_path, selected_content = _select_compose_file(key_files, "apps/web")

    assert selected_path == "apps/web/docker-compose.yml"
    assert "web" in selected_content


def test_build_repo_report_includes_compose_artifact_score():
    repo_result = {
        "repo": "example/repo",
        "package_path": ".",
        "error": None,
        "services": [{"name": "web", "build_context": ".", "port": 3000}],
        "stack_summary": "Node.js, Next.js",
        "stack_tokens": ["node", "next"],
        "key_files": {
                        "Dockerfile": """
FROM node:20-alpine
USER node
EXPOSE 3000
HEALTHCHECK CMD wget -qO- http://localhost:3000 || exit 1
""",
            "docker-compose.yml": """
services:
  web:
    build: .
    ports:
      - \"3000:3000\"
""",
                        "nginx.conf": """
events { worker_connections 1024; }
http {
    server {
        location / {
            proxy_pass http://web:3000;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection \"upgrade\";
            add_header X-Content-Type-Options \"nosniff\" always;
            add_header X-Frame-Options \"SAMEORIGIN\" always;
            add_header Content-Security-Policy \"default-src 'self'\" always;
        }
    }
}
""",
        },
    }
    label = {
        "expected_services": [{"name": "web", "build_context": "."}],
        "excluded_services": [],
        "required_stack_tokens": ["node"],
        "expected_ports": {"web": 3000},
    }

    report = _build_repo_report(repo_result, label)

    compose = report["artifact_scores"]["compose"]
    assert compose["file_path"] == "docker-compose.yml"
    assert compose["total_score"] > 0.0
    assert "criteria_scores" in compose
    assert "criterion_reasons" in compose

    dockerfile = report["artifact_scores"]["dockerfile"]
    assert dockerfile["file_path"] == "Dockerfile"
    assert dockerfile["total_score"] > 0.0
    assert "criteria_scores" in dockerfile
    assert "criterion_reasons" in dockerfile

    nginx = report["artifact_scores"]["nginx"]
    assert nginx["file_path"] == "nginx.conf"
    assert nginx["total_score"] > 0.0
    assert "criteria_scores" in nginx
    assert "criterion_reasons" in nginx


def test_select_dockerfile_prefers_package_local_file():
    key_files = {
        "Dockerfile": "FROM node:20-alpine",
        "apps/web/Dockerfile": "FROM node:20-alpine\nEXPOSE 3000",
    }

    selected_path, selected_content = _select_dockerfile(key_files, "apps/web")

    assert selected_path == "apps/web/Dockerfile"
    assert "EXPOSE 3000" in selected_content


def test_select_nginx_file_prefers_package_local_file():
    key_files = {
        "nginx.conf": "events {}",
        "apps/web/nginx/default.conf": "events {}\nhttp { server { location / { proxy_pass http://web:3000; } } }",
    }

    selected_path, selected_content = _select_nginx_file(key_files, "apps/web")

    assert selected_path == "apps/web/nginx/default.conf"
    assert "proxy_pass" in selected_content


def test_build_artifact_summary_includes_nginx_and_combined():
    scored_reports = [
        {
            "artifact_scores": {
                "dockerfile": {"file_path": "Dockerfile", "total_score": 1.0, "passed_threshold": True},
                "compose": {"file_path": "docker-compose.yml", "total_score": 0.9, "passed_threshold": True},
                "nginx": {"file_path": "nginx.conf", "total_score": 0.8, "passed_threshold": False},
            }
        },
        {
            "artifact_scores": {
                "dockerfile": {"file_path": None, "total_score": 0.0, "passed_threshold": False},
                "compose": {"file_path": "compose.yml", "total_score": 1.0, "passed_threshold": True},
                "nginx": {"file_path": "default.conf", "total_score": 1.0, "passed_threshold": True},
            }
        },
    ]

    summary = _build_artifact_summary(scored_reports)

    assert summary["dockerfile"]["scored_repo_count"] == 1
    assert summary["compose"]["scored_repo_count"] == 2
    assert summary["nginx"]["scored_repo_count"] == 2
    assert summary["combined"]["scored_repo_count"] == 2
    assert summary["combined"]["all_present_artifacts_pass_rate"] == 0.5


def test_build_generated_artifact_scores_handles_generated_content():
    generated = {
        "dockerfiles": {
            "Dockerfile": "FROM node:20-alpine\nUSER node\nEXPOSE 3000\nHEALTHCHECK CMD wget -qO- http://localhost:3000 || exit 1",
            "api/Dockerfile": "FROM python:3.12-slim\nUSER app\nEXPOSE 8000\nHEALTHCHECK CMD curl -f http://localhost:8000/health || exit 1",
        },
        "docker_compose": """
services:
    web:
        build: .
        ports:
            - \"3000:3000\"
""",
        "nginx_conf": """
events { worker_connections 1024; }
http {
    server {
        location / {
            proxy_pass http://web:3000;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection \"upgrade\";
            add_header X-Content-Type-Options \"nosniff\" always;
            add_header X-Frame-Options \"SAMEORIGIN\" always;
            add_header Content-Security-Policy \"default-src 'self'\" always;
        }
    }
}
""",
    }
    label = {
        "expected_services": [{"name": "web", "build_context": "."}],
        "required_stack_tokens": [],
    }

    scores = _build_generated_artifact_scores(generated, label)

    assert scores["dockerfile"]["file_path"] == "__generated_dockerfiles__"
    assert scores["compose"]["file_path"] == "__generated_docker_compose__"
    assert scores["nginx"]["file_path"] == "__generated_nginx_conf__"
    assert "web" in scores["dockerfile"]["per_service"]
    assert "api" in scores["dockerfile"]["per_service"]


def test_build_generated_artifact_scores_handles_missing_generated_content():
    generated = {
        "dockerfiles": {},
        "docker_compose": "",
        "nginx_conf": "",
    }
    label = {
        "expected_services": [{"name": "web", "build_context": "."}],
        "required_stack_tokens": ["node"],
    }

    scores = _build_generated_artifact_scores(generated, label)

    assert scores["dockerfile"]["file_path"] is None
    assert scores["dockerfile"]["total_score"] == 0.0
    assert scores["compose"]["file_path"] is None
    assert scores["compose"]["total_score"] == 0.0
    assert scores["nginx"]["file_path"] is None
    assert scores["nginx"]["total_score"] == 0.0


def test_build_generated_artifact_scores_uses_runtime_tokens_for_multi_service():
    generated = {
        "services": [
            {"name": "backend", "build_context": "apps/backend", "port": 5000},
            {"name": "web", "build_context": "apps/web", "port": 3000},
        ],
        "dockerfiles": {
            "apps/backend/Dockerfile": "FROM node:20-alpine\nUSER node\nEXPOSE 5000\nHEALTHCHECK CMD wget -qO- http://localhost:5000 || exit 1",
            "apps/web/Dockerfile": "FROM node:20-alpine\nUSER node\nEXPOSE 3000\nHEALTHCHECK CMD wget -qO- http://localhost:3000 || exit 1",
        },
        "docker_compose": "",
        "nginx_conf": "",
    }
    label = {
        "required_stack_tokens": ["node", "react"],
        "expected_services": [
            {"name": "backend", "build_context": "apps/backend"},
            {"name": "web", "build_context": "apps/web"},
        ],
    }

    scores = _build_generated_artifact_scores(generated, label)

    assert scores["dockerfile"]["per_service"]["backend"]["criteria_scores"]["stack_alignment"] == 1.0
    assert scores["dockerfile"]["per_service"]["web"]["criteria_scores"]["stack_alignment"] == 1.0


def test_build_generated_artifact_scores_filters_framework_tokens_for_single_service():
    # Even for a single-service repo, non-runtime tokens like 'react' should not be required
    # in the Dockerfile — the Dockerfile only uses the node runtime.
    generated = {
        "services": [{"name": "web", "build_context": "apps/web", "port": 3000}],
        "dockerfiles": {
            "Dockerfile": "FROM node:20-alpine\nUSER node\nEXPOSE 3000\nHEALTHCHECK CMD wget -qO- http://localhost:3000 || exit 1",
        },
        "docker_compose": "",
        "nginx_conf": "",
    }
    label = {
        "required_stack_tokens": ["react"],
        "expected_services": [{"name": "web", "build_context": "apps/web"}],
    }

    scores = _build_generated_artifact_scores(generated, label)

    assert scores["dockerfile"]["per_service"]["apps/web/Dockerfile"]["criteria_scores"]["stack_alignment"] == 1.0, (
        "react is a framework token and should not be required in the Dockerfile"
    )


def test_compose_generation_audit_detects_missing_when_required():
    label = {
        "expected_services": [
            {"name": "web", "build_context": "."},
            {"name": "api", "build_context": "./api"},
        ]
    }
    generated_artifact_scores = {
        "compose": {
            "file_path": None,
        }
    }

    audit = _compose_generation_audit(label, generated_artifact_scores)

    assert audit["compose_required"] is True
    assert audit["compose_generated"] is False
    assert audit["wrong_compose_gen"] is True
    assert audit["reason"] == "compose_missing_when_required"


def test_compose_generation_audit_detects_generated_when_not_required():
    label = {
        "expected_services": [
            {"name": "web", "build_context": "."},
        ]
    }
    generated_artifact_scores = {
        "compose": {
            "file_path": "__generated_docker_compose__",
        }
    }

    audit = _compose_generation_audit(label, generated_artifact_scores)

    assert audit["compose_required"] is False
    assert audit["compose_generated"] is True
    assert audit["wrong_compose_gen"] is True
    assert audit["reason"] == "compose_generated_when_not_required"


def test_summarize_compose_generation_audits_counts_both_error_types():
    audits = [
        {
            "wrong_compose_gen": True,
            "reason": "compose_missing_when_required",
        },
        {
            "wrong_compose_gen": True,
            "reason": "compose_generated_when_not_required",
        },
        {
            "wrong_compose_gen": False,
            "reason": "ok",
        },
    ]

    summary = _summarize_compose_generation_audits(audits)

    assert summary["compose_generation_eval_repo_count"] == 3
    assert summary["wrong_compose_gen_count"] == 2
    assert summary["wrong_compose_gen_rate"] == 2 / 3
    assert summary["compose_missing_when_required_count"] == 1
    assert summary["compose_generated_when_not_required_count"] == 1


def test_evaluate_targets_parallel_preserves_target_order(monkeypatch):
    targets = [
        {
            "repo": "slow/repo",
            "repo_url": "https://github.com/slow/repo",
            "package_path": ".",
            "label": {},
        },
        {
            "repo": "fast/repo",
            "repo_url": "https://github.com/fast/repo",
            "package_path": ".",
            "label": {},
        },
    ]

    def fake_run_planner_for_repo(repo, repo_url, github_token, max_files, package_path="."):
        if repo == "slow/repo":
            time.sleep(0.03)
        return {
            "repo": repo,
            "repo_url": repo_url,
            "error": None,
            "services": [],
            "stack_summary": "",
            "stack_tokens": [],
            "package_path": package_path,
            "key_files": {},
        }

    monkeypatch.setattr(evaluate_scan_quality, "_run_planner_for_repo", fake_run_planner_for_repo)
    monkeypatch.setattr(evaluate_scan_quality, "score_repo", lambda **kwargs: {"repo": kwargs["repo"]})
    monkeypatch.setattr(
        evaluate_scan_quality,
        "_build_repo_report",
        lambda result, label: {
            "repo": result["repo"],
            "error": result.get("error"),
            "artifact_scores": {},
        },
    )

    evaluations = _evaluate_targets(
        targets,
        github_token=None,
        max_files=10,
        include_generated=False,
        max_workers=2,
    )

    assert [entry["repo_report"]["repo"] for entry in evaluations] == ["slow/repo", "fast/repo"]
    assert [entry["score_obj"]["repo"] for entry in evaluations] == ["slow/repo", "fast/repo"]