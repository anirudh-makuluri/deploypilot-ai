"""
V2-08 regression fixture suite for artifact-level evaluators:
  score_dockerfile, score_compose, score_nginx
"""

from tools.eval_metrics import (
    ARTIFACT_SCORE_WEIGHTS,
    score_compose,
    score_dockerfile,
    score_nginx,
)

# ---------------------------------------------------------------------------
# Fixtures — Dockerfile
# ---------------------------------------------------------------------------

_DOCKERFILE_FULL = """\
FROM node:20-alpine AS base
WORKDIR /app
COPY package*.json ./
RUN npm ci --only=production
USER node
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://localhost:3000/health || exit 1
CMD ["node", "server.js"]
"""

_DOCKERFILE_NO_USER = """\
FROM node:20-alpine
WORKDIR /app
EXPOSE 3000
HEALTHCHECK CMD wget -qO- http://localhost:3000 || exit 1
CMD ["node", "server.js"]
"""

_DOCKERFILE_NO_HEALTHCHECK = """\
FROM python:3.12-slim
WORKDIR /app
USER appuser
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
"""

_DOCKERFILE_MULTI_STAGE = """\
FROM node:20-alpine AS builder
WORKDIR /app
COPY . .
RUN npm ci && npm run build

FROM node:20-alpine AS runtime
WORKDIR /app
COPY --from=builder /app/dist ./dist
USER node
EXPOSE 3000
HEALTHCHECK CMD wget -qO- http://localhost:3000 || exit 1
CMD ["node", "dist/index.js"]
"""

_DOCKERFILE_NO_STACK_MATCH = """\
FROM node:20-alpine
USER node
EXPOSE 3000
HEALTHCHECK CMD wget -qO- http://localhost:3000 || exit 1
"""


def test_score_dockerfile_full_score_single_service():
    score = score_dockerfile(_DOCKERFILE_FULL, required_stack_tokens=["node"])
    assert score.passed_threshold is True
    assert score.total_score == 1.0
    assert score.criteria_scores["base_image"] == 1.0
    assert score.criteria_scores["non_root_user"] == 1.0
    assert score.criteria_scores["expose_or_documented_port"] == 1.0
    assert score.criteria_scores["healthcheck"] == 1.0
    assert score.criteria_scores["stack_alignment"] == 1.0


def test_score_dockerfile_missing_non_root_user_reduces_score():
    score = score_dockerfile(_DOCKERFILE_NO_USER, required_stack_tokens=["node"])
    assert score.criteria_scores["non_root_user"] == 0.0
    assert score.criteria_scores["base_image"] == 1.0
    assert score.criteria_scores["healthcheck"] == 1.0
    expected_total = round(
        1.0 * ARTIFACT_SCORE_WEIGHTS["dockerfile"]["base_image"]
        + 0.0 * ARTIFACT_SCORE_WEIGHTS["dockerfile"]["non_root_user"]
        + 1.0 * ARTIFACT_SCORE_WEIGHTS["dockerfile"]["expose_or_documented_port"]
        + 1.0 * ARTIFACT_SCORE_WEIGHTS["dockerfile"]["healthcheck"]
        + 1.0 * ARTIFACT_SCORE_WEIGHTS["dockerfile"]["stack_alignment"],
        6,
    )
    assert score.total_score == expected_total
    assert score.passed_threshold is False


def test_score_dockerfile_missing_healthcheck_at_threshold_boundary():
    # base(0.225) + non_root(0.225) + expose(0.225) + healthcheck(0) + stack(0.225) = 0.90
    score = score_dockerfile(_DOCKERFILE_NO_HEALTHCHECK, required_stack_tokens=["python"])
    assert score.criteria_scores["healthcheck"] == 0.0
    assert round(score.total_score, 6) == round(
        ARTIFACT_SCORE_WEIGHTS["dockerfile"]["base_image"]
        + ARTIFACT_SCORE_WEIGHTS["dockerfile"]["non_root_user"]
        + ARTIFACT_SCORE_WEIGHTS["dockerfile"]["expose_or_documented_port"]
        + ARTIFACT_SCORE_WEIGHTS["dockerfile"]["stack_alignment"],
        6,
    )
    assert score.passed_threshold is True


def test_score_dockerfile_multi_stage_build_scores_correctly():
    score = score_dockerfile(_DOCKERFILE_MULTI_STAGE, required_stack_tokens=["node"])
    assert score.criteria_scores["base_image"] == 1.0
    assert score.criteria_scores["non_root_user"] == 1.0
    assert score.criteria_scores["expose_or_documented_port"] == 1.0
    assert score.criteria_scores["healthcheck"] == 1.0
    assert score.criteria_scores["stack_alignment"] == 1.0
    assert score.total_score == 1.0
    assert score.passed_threshold is True


def test_score_dockerfile_stack_token_mismatch_penalises_alignment():
    score = score_dockerfile(_DOCKERFILE_NO_STACK_MATCH, required_stack_tokens=["python", "fastapi"])
    assert score.criteria_scores["stack_alignment"] == 0.0
    assert score.passed_threshold is False


def test_score_dockerfile_empty_content_returns_zero():
    score = score_dockerfile("")
    assert score.total_score == 0.0
    assert score.passed_threshold is False
    for reason in score.criterion_reasons.values():
        assert "Empty" in reason or "missing" in reason.lower()


def test_score_dockerfile_no_stack_tokens_treats_alignment_as_pass():
    score = score_dockerfile(_DOCKERFILE_FULL, required_stack_tokens=None)
    assert score.criteria_scores["stack_alignment"] == 1.0


# ---------------------------------------------------------------------------
# Fixtures — Compose
# ---------------------------------------------------------------------------

_COMPOSE_SINGLE_SERVICE_FULL = """\
services:
  web:
    build:
      context: .
    ports:
      - "3000:3000"
"""

_COMPOSE_MULTI_SERVICE_FULL = """\
services:
  web:
    build:
      context: frontend
    ports:
      - "3000:3000"
    environment:
      - NODE_ENV=production
  api:
    build:
      context: backend
    ports:
      - "8000:8000"
    environment:
      NODE_ENV: production
"""

_COMPOSE_MISSING_SERVICE = """\
services:
  web:
    build: .
    ports:
      - "3000:3000"
"""

_COMPOSE_WRONG_BUILD_CONTEXT = """\
services:
  web:
    build:
      context: wrong-path
    ports:
      - "3000:3000"
"""

_COMPOSE_NO_PORTS = """\
services:
  web:
    build: .
"""

_COMPOSE_WITH_VOLUMES = """\
services:
  db:
    image: postgres:15
    volumes:
      - db_data:/var/lib/postgresql/data
volumes:
  db_data:
"""


def test_score_compose_single_service_full_score():
    score = score_compose(
        _COMPOSE_SINGLE_SERVICE_FULL,
        expected_services=[{"name": "web", "build_context": "."}],
    )
    assert score.passed_threshold is True
    assert score.criteria_scores["syntax_validity"] == 1.0
    assert score.criteria_scores["service_coverage"] == 1.0
    assert score.criteria_scores["build_context_validity"] == 1.0
    assert score.criteria_scores["port_mappings"] == 1.0
    assert score.total_score == 1.0


def test_score_compose_multi_service_all_covered():
    score = score_compose(
        _COMPOSE_MULTI_SERVICE_FULL,
        expected_services=[
            {"name": "web", "build_context": "frontend"},
            {"name": "api", "build_context": "backend"},
        ],
    )
    assert score.criteria_scores["service_coverage"] == 1.0
    assert score.criteria_scores["build_context_validity"] == 1.0
    assert score.criteria_scores["port_mappings"] == 1.0
    assert score.criteria_scores["env_placeholders"] == 1.0
    assert score.passed_threshold is True


def test_score_compose_missing_expected_service_reduces_coverage():
    # Expected: [web, api] — compose only has [web]
    score = score_compose(
        _COMPOSE_MISSING_SERVICE,
        expected_services=[
            {"name": "web", "build_context": "."},
            {"name": "api", "build_context": "./api"},
        ],
    )
    assert score.criteria_scores["service_coverage"] == 0.5
    assert score.passed_threshold is False


def test_score_compose_wrong_build_context_penalises_validity():
    score = score_compose(
        _COMPOSE_WRONG_BUILD_CONTEXT,
        expected_services=[{"name": "web", "build_context": "."}],
    )
    assert score.criteria_scores["build_context_validity"] == 0.0
    assert score.passed_threshold is False


def test_score_compose_no_ports_reduces_port_score():
    score = score_compose(
        _COMPOSE_NO_PORTS,
        expected_services=[{"name": "web", "build_context": "."}],
    )
    assert score.criteria_scores["port_mappings"] == 0.0


def test_score_compose_malformed_yaml_returns_zero():
    score = score_compose("services: {\nbadyaml: [unclosed")
    assert score.total_score == 0.0
    assert score.passed_threshold is False
    assert "syntax" in score.criterion_reasons["syntax_validity"].lower()


def test_score_compose_with_valid_volumes_passes_hygiene():
    score = score_compose(_COMPOSE_WITH_VOLUMES)
    assert score.criteria_scores["volume_hygiene"] == 1.0
    assert score.criteria_scores["syntax_validity"] == 1.0


def test_score_compose_empty_content_returns_zero():
    score = score_compose("")
    assert score.total_score == 0.0
    assert score.passed_threshold is False


# ---------------------------------------------------------------------------
# Fixtures — Nginx
# ---------------------------------------------------------------------------

_NGINX_FULL = """\
events { worker_connections 1024; }
http {
    server {
        listen 80;
        location / {
            proxy_pass http://web:3000;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            add_header X-Content-Type-Options "nosniff" always;
            add_header X-Frame-Options "SAMEORIGIN" always;
            add_header Content-Security-Policy "default-src 'self'" always;
        }
    }
}
"""

_NGINX_NO_SECURITY_HEADERS = """\
events { worker_connections 1024; }
http {
    server {
        location / {
            proxy_pass http://web:3000;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
        }
    }
}
"""

_NGINX_NO_PROXY_PASS = """\
events { worker_connections 1024; }
http {
    server {
        listen 80;
        root /var/www/html;
        index index.html;
    }
}
"""

_NGINX_MULTI_SERVICE = """\
events { worker_connections 1024; }
http {
    upstream frontend { server web:3000; }
    upstream backend  { server api:8000; }
    server {
        location / {
            proxy_pass http://frontend;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            add_header X-Content-Type-Options "nosniff" always;
            add_header X-Frame-Options "SAMEORIGIN" always;
            add_header Content-Security-Policy "default-src 'self'" always;
        }
        location /api/ {
            proxy_pass http://backend;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
        }
    }
}
"""

_NGINX_UNBALANCED_BRACES = """\
events { worker_connections 1024; }
http {
    server {
        location / {
            proxy_pass http://web:3000;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            add_header X-Content-Type-Options "nosniff" always;
            add_header X-Frame-Options "SAMEORIGIN" always;
            add_header Content-Security-Policy "default-src 'self'" always;
"""


def test_score_nginx_full_score_single_service():
    score = score_nginx(
        _NGINX_FULL,
        expected_services=[{"name": "web"}],
    )
    assert score.total_score == 1.0
    assert score.passed_threshold is True
    assert score.criteria_scores["route_coverage"] == 1.0
    assert score.criteria_scores["proxy_correctness"] == 1.0
    assert score.criteria_scores["security_headers"] == 1.0
    assert score.criteria_scores["websocket_handling"] == 1.0
    assert score.criteria_scores["syntax_sanity"] == 1.0


def test_score_nginx_missing_security_headers_reduces_score():
    score = score_nginx(
        _NGINX_NO_SECURITY_HEADERS,
        expected_services=[{"name": "web"}],
    )
    assert score.criteria_scores["security_headers"] == 0.0
    assert score.criteria_scores["proxy_correctness"] == 1.0
    expected_without_headers = round(
        1.0 * ARTIFACT_SCORE_WEIGHTS["nginx"]["route_coverage"]
        + 1.0 * ARTIFACT_SCORE_WEIGHTS["nginx"]["proxy_correctness"]
        + 0.0 * ARTIFACT_SCORE_WEIGHTS["nginx"]["security_headers"]
        + 1.0 * ARTIFACT_SCORE_WEIGHTS["nginx"]["websocket_handling"]
        + 1.0 * ARTIFACT_SCORE_WEIGHTS["nginx"]["syntax_sanity"],
        6,
    )
    assert score.total_score == expected_without_headers
    assert score.passed_threshold is False


def test_score_nginx_no_proxy_pass_gives_zero_proxy_correctness():
    score = score_nginx(_NGINX_NO_PROXY_PASS)
    assert score.criteria_scores["proxy_correctness"] == 0.0
    assert score.criteria_scores["route_coverage"] == 0.0


def test_score_nginx_multi_service_route_coverage():
    score = score_nginx(
        _NGINX_MULTI_SERVICE,
        expected_services=[
            {"name": "web"},
            {"name": "api"},
        ],
    )
    assert score.criteria_scores["route_coverage"] == 1.0
    assert score.criteria_scores["proxy_correctness"] == 1.0
    assert score.criteria_scores["security_headers"] == 1.0


def test_score_nginx_unbalanced_braces_reduces_syntax_sanity():
    score = score_nginx(_NGINX_UNBALANCED_BRACES, expected_services=[{"name": "web"}])
    # braces_ok = False; has_server_block = True; has_events_or_http = True → 2/3
    assert score.criteria_scores["syntax_sanity"] == round(2 / 3, 6)


def test_score_nginx_empty_content_returns_zero():
    score = score_nginx("")
    assert score.total_score == 0.0
    assert score.passed_threshold is False
    for reason in score.criterion_reasons.values():
        assert "empty" in reason.lower() or "missing" in reason.lower()


def test_score_nginx_partial_websocket_settings_produces_partial_score():
    # Only has proxy_http_version 1.1, no Upgrade/Connection headers
    partial_ws = """\
events { worker_connections 1024; }
http {
    server {
        location / {
            proxy_pass http://web:3000;
            proxy_http_version 1.1;
            add_header X-Content-Type-Options "nosniff" always;
            add_header X-Frame-Options "SAMEORIGIN" always;
            add_header Content-Security-Policy "default-src 'self'" always;
        }
    }
}
"""
    score = score_nginx(partial_ws, expected_services=[{"name": "web"}])
    # has_upgrade=False, has_connection_upgrade=False, has_http11=True → 1/3
    assert score.criteria_scores["websocket_handling"] == round(1 / 3, 6)
    assert score.criteria_scores["security_headers"] == 1.0
