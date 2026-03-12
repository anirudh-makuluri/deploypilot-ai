"""Dockerfile evaluator tests."""
from tools.eval_metrics import score_dockerfile


def test_score_dockerfile_empty_content():
    score = score_dockerfile("")
    assert score.total_score == 0.0
    assert score.passed_threshold is False


def test_score_dockerfile_all_criteria_met():
    content = """
    FROM python:3.11-slim
    USER appuser
    EXPOSE 8000
    HEALTHCHECK CMD curl http://localhost:8000
    """
    score = score_dockerfile(content, required_stack_tokens=["python"])

    assert score.criteria_scores["base_image"] == 1.0
    assert score.criteria_scores["non_root_user"] == 1.0
    assert score.criteria_scores["expose_or_documented_port"] == 1.0
    assert score.criteria_scores["healthcheck"] == 1.0
    assert score.criteria_scores["stack_alignment"] == 1.0
    assert score.total_score == 1.0
    assert score.passed_threshold is True


def test_score_dockerfile_missing_non_root_user():
    content = """
    FROM python:3.11-slim
    EXPOSE 8000
    """
    score = score_dockerfile(content, required_stack_tokens=["python"])

    assert score.criteria_scores["base_image"] == 1.0
    assert score.criteria_scores["non_root_user"] == 0.0
    assert score.total_score < 1.0
    assert "No non-root USER" in score.criterion_reasons["non_root_user"]


def test_score_dockerfile_missing_stack_tokens():
    content = """
    FROM node:18-alpine
    USER appuser
    EXPOSE 3000
    HEALTHCHECK CMD curl http://localhost:3000
    """
    score = score_dockerfile(content, required_stack_tokens=["python", "fastapi"])

    assert score.criteria_scores["stack_alignment"] == 0.0
    assert score.passed_threshold is False


def test_score_dockerfile_with_root_user():
    content = """
    FROM python:3.11-slim
    USER root
    EXPOSE 8000
    HEALTHCHECK CMD curl http://localhost:8000
    """
    score = score_dockerfile(content, required_stack_tokens=["python"])

    assert score.criteria_scores["non_root_user"] == 0.0
    assert score.criteria_scores["base_image"] == 1.0
