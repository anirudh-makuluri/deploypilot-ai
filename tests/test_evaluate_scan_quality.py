import json

from tools.evaluate_scan_quality import _failure_bucket_from_report, _load_labels


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