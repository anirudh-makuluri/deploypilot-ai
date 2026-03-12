"""Compose evaluator tests for V2-04."""
from tools.eval_metrics import score_compose


def test_score_compose_valid_multiservice():
    content = """
    version: '3.8'
    services:
      web:
        build: .
        ports:
          - "3000:3000"
        environment:
          NODE_ENV: production
        volumes:
          - ./data:/app/data
      backend:
        build: ./api
        ports:
          - "8000:8000"
        environment:
          DATABASE_URL: postgres://localhost
    """
    expected_services = [
        {"name": "web", "build_context": "."},
        {"name": "backend", "build_context": "./api"}
    ]
    score = score_compose(content, expected_services)

    assert score.criteria_scores["syntax_validity"] == 1.0
    assert score.criteria_scores["service_coverage"] == 1.0
    assert score.criteria_scores["build_context_validity"] == 1.0
    assert score.criteria_scores["port_mappings"] == 1.0
    assert score.criteria_scores["env_placeholders"] == 1.0
    assert score.criteria_scores["volume_hygiene"] == 1.0
    assert score.total_score == 1.0
    assert score.passed_threshold is True


def test_score_compose_invalid_yaml():
    content = """
    version: 3.8
    services:
      web:
        build: .
        ports: [invalid
    """
    score = score_compose(content)

    assert score.criteria_scores["syntax_validity"] == 0.0
    assert score.passed_threshold is False


def test_score_compose_missing_services():
    content = """
    version: '3.8'
    services:
      web:
        build: .
    """
    expected_services = [
        {"name": "web", "build_context": "."},
        {"name": "backend", "build_context": "./api"}
    ]
    score = score_compose(content, expected_services)

    assert score.criteria_scores["service_coverage"] == 0.5
    assert score.passed_threshold is False


def test_score_compose_missing_ports():
    content = """
    version: '3.8'
    services:
      api:
        build: .
        environment:
          PORT: 8000
    """
    score = score_compose(content)

    assert score.criteria_scores["port_mappings"] == 0.0
    assert score.total_score < 1.0


def test_score_compose_empty_content():
    score = score_compose("")
    assert score.criteria_scores["syntax_validity"] == 0.0
    assert score.passed_threshold is False


def test_score_compose_minimal_valid():
    content = """
    version: '3.8'
    services:
      web:
        image: nginx:latest
        ports:
          - "80:80"
    """
    score = score_compose(content)
    
    assert score.criteria_scores["syntax_validity"] == 1.0
    assert score.criteria_scores["port_mappings"] == 1.0
    assert score.criteria_scores["build_context_validity"] == 0.0
