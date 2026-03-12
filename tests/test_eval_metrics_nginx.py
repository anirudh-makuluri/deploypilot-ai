"""Nginx evaluator tests for V2-05."""
from tools.eval_metrics import score_nginx


def test_score_nginx_empty_content():
    score = score_nginx("")

    assert score.criteria_scores["syntax_sanity"] == 0.0
    assert score.total_score == 0.0
    assert score.passed_threshold is False


def test_score_nginx_valid_proxy_config():
    content = """
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

        location /api/ {
          proxy_pass http://api:8000;
        }
      }
    }
    """
    expected_services = [
        {"name": "web", "build_context": "."},
        {"name": "api", "build_context": "./api"},
    ]

    score = score_nginx(content, expected_services=expected_services)

    assert score.criteria_scores["route_coverage"] == 1.0
    assert score.criteria_scores["proxy_correctness"] == 1.0
    assert score.criteria_scores["security_headers"] == 1.0
    assert score.criteria_scores["websocket_handling"] == 1.0
    assert score.criteria_scores["syntax_sanity"] == 1.0
    assert score.total_score == 1.0
    assert score.passed_threshold is True


def test_score_nginx_missing_expected_service_route():
    content = """
    events { worker_connections 1024; }
    http {
      server {
        location / {
          proxy_pass http://web:3000;
        }
      }
    }
    """
    expected_services = [
        {"name": "web", "build_context": "."},
        {"name": "api", "build_context": "./api"},
    ]

    score = score_nginx(content, expected_services=expected_services)

    assert score.criteria_scores["route_coverage"] == 0.5
    assert score.passed_threshold is False


def test_score_nginx_invalid_proxy_target():
    content = """
    events { worker_connections 1024; }
    http {
      server {
        location / {
          proxy_pass backend:3000;
        }
      }
    }
    """

    score = score_nginx(content)

    assert score.criteria_scores["proxy_correctness"] == 0.0
    assert score.passed_threshold is False


def test_score_nginx_partial_websocket_settings():
    content = """
    events { worker_connections 1024; }
    http {
      server {
        location /socket {
          proxy_pass http://web:3000;
          proxy_set_header Upgrade $http_upgrade;
        }
      }
    }
    """

    score = score_nginx(content)

    assert score.criteria_scores["websocket_handling"] == 0.333333
    assert score.passed_threshold is False


def test_score_nginx_unbalanced_braces_reduces_syntax_sanity():
    content = """
    events { worker_connections 1024; }
    http {
      server {
        location / {
          proxy_pass http://web:3000;
        }
    }
    """

    score = score_nginx(content)

    assert score.criteria_scores["syntax_sanity"] < 1.0
    assert score.passed_threshold is False
