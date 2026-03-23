from graph.nodes.verifier import _compute_deterministic_confidence, _filter_risks


def test_filter_risks_removes_generic_non_blockers():
    risks = [
        "Hadolint warns about not pinning apk package versions in Dockerfiles",
        "Multiple consecutive RUN instructions in Dockerfiles could be consolidated",
        "No explicit volume mapping for persistent data or logs",
        "Missing explicit health check endpoint verification for backend service",
        "Potential security risk: using environment variables for sensitive credentials without explicit secret management",
    ]

    services = [
        {"name": "backend", "port": 5000},
        {"name": "web", "port": 3000},
    ]
    dockerfiles = {
        "backend": "FROM node:20-alpine\nHEALTHCHECK CMD wget -qO- http://localhost:5000 || exit 1\n",
        "web": "FROM node:20-alpine\n",
    }
    compose = """
services:
  backend:
    environment:
      - NODE_ENV=production
  web:
    environment:
      - NEXT_PUBLIC_BACKEND_URL=${NEXT_PUBLIC_BACKEND_URL}
"""

    filtered = _filter_risks(risks, services, dockerfiles, compose, "")

    assert filtered == []


def test_filter_risks_keeps_actionable_stateful_volume_warning():
    risks = ["No explicit volume mapping for persistent data or logs"]
    services = [
        {"name": "postgres", "port": 5432},
        {"name": "api", "port": 8000},
    ]

    filtered = _filter_risks(risks, services, {"api": "FROM python:3.11"}, "services:\n  postgres:\n    image: postgres", "")

    assert filtered == risks


def test_filter_risks_keeps_secret_warning_when_hardcoded_value_detected():
    risks = ["Potential security risk: using environment variables for sensitive credentials without explicit secret management"]
    services = [{"name": "api", "port": 8000}]
    compose = """
services:
  api:
    environment:
      - DB_PASSWORD=supersecret
"""

    filtered = _filter_risks(risks, services, {"api": "FROM python:3.11"}, compose, "")

    assert filtered == risks


def test_deterministic_confidence_high_when_no_risks_and_complete_artifacts():
    services = [
        {"name": "backend", "port": 5000},
        {"name": "web", "port": 3000},
    ]
    dockerfiles = {
        "backend": "FROM node:20-alpine\n",
        "web": "FROM node:20-alpine\n",
    }
    compose = "services:\n  backend:\n    build: .\n  web:\n    build: .\n"
    nginx = "http { server { location / { proxy_pass http://web:3000; } } }"

    confidence = _compute_deterministic_confidence(services, dockerfiles, compose, nginx, [])

    assert confidence == 0.99


def test_deterministic_confidence_drops_with_risks_and_missing_artifacts():
    services = [
        {"name": "backend", "port": 5000},
        {"name": "web", "port": None},
    ]
    dockerfiles = {
        "backend": "FROM node:20-alpine\n",
    }

    confidence = _compute_deterministic_confidence(
        services,
        dockerfiles,
        docker_compose="",
        nginx_conf="",
        risks=["risk-1", "risk-2", "risk-3"],
    )

    assert confidence < 0.6


def test_filter_risks_drops_localhost_nginx_warning_for_host_nginx_model():
    risks = ["NGINX config uses localhost for upstream servers, which may not work in containerized environment"]
    services = [
        {"name": "backend", "port": 5000},
        {"name": "web", "port": 3000},
    ]
    compose = """
services:
    backend:
        ports:
            - 5000:5000
    web:
        ports:
            - 3000:3000
"""
    nginx = "location / { proxy_pass http://localhost:3000; }"

    filtered = _filter_risks(risks, services, {}, compose, nginx)

    assert filtered == []


def test_filter_risks_drops_hadolint_unversioned_wording():
    risks = ["Hadolint warns about unversioned package installations in Dockerfiles"]
    filtered = _filter_risks(risks, [{"name": "api", "port": 8000}], {"api": "FROM node:20"}, "", "")
    assert filtered == []


def test_filter_risks_drops_compose_env_placeholder_warning():
    risks = ["Docker-compose uses environment variables that are not explicitly defined in the file"]
    filtered = _filter_risks(risks, [{"name": "api", "port": 8000}], {"api": "FROM node:20"}, "services:\n  api:\n    environment:\n      - SECRET=${SECRET}", "")
    assert filtered == []


def test_filter_risks_drops_missing_compose_warning_when_compose_not_required():
    risks = ["docker-compose.yml is missing, but required for deployment"]
    services = [
        {"name": "web", "build_context": "apps/web", "port": 3000},
        {"name": "web-worker", "build_context": "apps/web", "port": 3001},
    ]

    filtered = _filter_risks(
        risks,
        services,
        {"web": "FROM node:20"},
        docker_compose="",
        nginx_conf="",
        package_path="apps/web",
    )

    assert filtered == []


def test_deterministic_confidence_does_not_penalize_missing_compose_when_not_required():
    services = [
        {"name": "web", "build_context": "apps/web", "port": 3000},
        {"name": "web-worker", "build_context": "apps/web", "port": 3001},
    ]
    dockerfiles = {
        "web": "FROM node:20-alpine\n",
        "web-worker": "FROM node:20-alpine\n",
    }

    confidence = _compute_deterministic_confidence(
        services,
        dockerfiles,
        docker_compose="",
        nginx_conf="",
        risks=[],
        package_path="apps/web",
    )

    assert confidence == 0.99
