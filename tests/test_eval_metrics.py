from tools.eval_metrics import (
    ARTIFACT_PASS_THRESHOLDS,
    ARTIFACT_SCORE_WEIGHTS,
    artifact_scoring_contract,
    score_repo,
    summarize_scores,
)


def test_score_repo_counts_tp_fp_fn_and_leakage():
    predicted = [
        {"name": "frontend-app", "build_context": ".", "port": 3000},
        {"name": "backend", "build_context": "services/api", "port": 8080},
        {"name": "mobile-shell", "build_context": "apps/mobile", "port": 19000},
    ]
    expected = [
        {"name": "web", "build_context": "."},
        {"name": "api", "build_context": "services/api"},
    ]
    excluded = [{"name": "mobile", "build_context": "apps/mobile"}]

    score = score_repo(
        repo="owner/repo",
        predicted_services=predicted,
        expected_services=expected,
        excluded_services=excluded,
        required_stack_tokens=["next", "node"],
        predicted_stack="Next.js Node app",
        predicted_stack_tokens=["node", "next"],
        expected_ports={"web": 3000, "api": 8080},
    )

    assert score.true_positives == 2
    assert score.false_positives == 1
    assert score.false_negatives == 0
    assert score.leaked_mobile_services == 1
    assert score.stack_match is True
    assert score.known_port_count == 2
    assert score.correct_port_count == 2
    assert score.missing_port_count == 0


def test_summarize_scores_computes_expected_ratios():
    score_a = score_repo(
        repo="a/b",
        predicted_services=[{"name": "app", "build_context": ".", "port": 3000}],
        expected_services=[{"name": "web", "build_context": "."}],
        excluded_services=[],
        required_stack_tokens=["next"],
        predicted_stack="Next.js app",
        predicted_stack_tokens=["next", "node"],
        expected_ports={"web": 3000},
    )

    score_b = score_repo(
        repo="c/d",
        predicted_services=[
            {"name": "backend-service", "build_context": "services/api", "port": 5000},
            {"name": "mobile-client", "build_context": "apps/mobile", "port": 19000},
        ],
        expected_services=[{"name": "api", "build_context": "services/api"}],
        excluded_services=[{"name": "mobile", "build_context": "apps/mobile"}],
        required_stack_tokens=["fastapi"],
        predicted_stack="Unknown",
        predicted_stack_tokens=[],
        expected_ports={"api": 8080},
    )

    summary = summarize_scores([score_a, score_b])

    assert summary["repos_scored"] == 2
    assert summary["service_true_positives"] == 2
    assert summary["service_false_positives"] == 1
    assert summary["service_false_negatives"] == 0
    assert summary["service_precision"] == 2 / 3
    assert summary["service_recall"] == 1.0
    assert summary["service_f1"] == 0.8
    assert summary["mobile_leakage_rate"] == 1 / 3
    assert summary["stack_labeled_repo_count"] == 2
    assert summary["stack_accuracy"] == 0.5
    assert summary["known_port_count"] == 2
    assert summary["correct_port_count"] == 1
    assert summary["port_accuracy_known"] == 0.5
    assert summary["port_unknown_rate"] == 0.0


def test_artifact_scoring_contract_is_versioned_and_complete():
    contract = artifact_scoring_contract()

    assert contract["artifact_score_schema_version"] == "v1"
    assert contract["weights"] == ARTIFACT_SCORE_WEIGHTS
    assert contract["thresholds"] == ARTIFACT_PASS_THRESHOLDS


def test_artifact_weights_are_normalized_per_artifact():
    for artifact, criteria in ARTIFACT_SCORE_WEIGHTS.items():
        assert criteria
        assert round(sum(criteria.values()), 6) == 1.0, artifact

