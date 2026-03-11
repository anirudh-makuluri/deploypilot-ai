from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from graph.nodes import planner_node
from tools.eval_metrics import score_repo, summarize_scores
from tools.github_tools import fetch_repo_structure_impl


def _repo_from_url_or_full_name(value: str) -> str:
    text = (value or "").strip()
    if "github.com/" in text:
        text = text.split("github.com/")[-1]
    return text.strip().strip("/")


def _repo_url(full_name: str) -> str:
    return f"https://github.com/{full_name}"


def _resolve_label_repo(row: Dict[str, Any]) -> tuple[str, str]:
    repo_url = (row.get("repo_url") or "").strip()
    repo = _repo_from_url_or_full_name(repo_url or row.get("repo", ""))
    normalized_repo_url = repo_url or (_repo_url(repo) if repo else "")
    return repo, normalized_repo_url


def _load_labels(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    repos = data.get("repos", []) if isinstance(data, dict) else []
    labels: List[Dict[str, Any]] = []
    for row in repos:
        if not isinstance(row, dict):
            continue
        repo, repo_url = _resolve_label_repo(row)
        if not repo or not repo_url:
            continue
        normalized = dict(row)
        normalized["repo"] = repo
        normalized["repo_url"] = repo_url
        normalized["package_path"] = (row.get("package_path") or ".").strip() or "."
        labels.append(normalized)
    return labels


def _run_planner_for_repo(repo: str, repo_url: str, github_token: Optional[str], max_files: int, package_path: str = ".") -> Dict[str, Any]:

    # Call the underlying implementation function directly
    scan = fetch_repo_structure_impl(
        repo_url=repo_url,
        github_token=github_token,
        max_files=max_files,
        package_path=package_path,
    )

    if "error" in scan:
        return {
            "repo": repo,
            "repo_url": repo_url,
            "error": scan.get("error"),
            "services": [],
            "stack_summary": "",
            "stack_tokens": [],
            "package_path": package_path,
        }

    state = {
        "repo_url": repo_url,
        "repo_scan": scan,
        "package_path": package_path,
    }
    planned = planner_node(state)
    return {
        "repo": repo,
        "repo_url": repo_url,
        "error": planned.get("error"),
        "services": planned.get("services", []),
        "stack_summary": planned.get("detected_stack", ""),
        "stack_tokens": planned.get("stack_tokens", []),
        "package_path": package_path,
    }


def _build_repo_report(repo_result: Dict[str, Any], label: Dict[str, Any]) -> Dict[str, Any]:
    score = score_repo(
        repo=repo_result["repo"],
        predicted_services=repo_result.get("services", []),
        expected_services=label.get("expected_services", []),
        excluded_services=label.get("excluded_services", []),
        required_stack_tokens=label.get("required_stack_tokens", []),
        predicted_stack=repo_result.get("stack_summary", ""),
        predicted_stack_tokens=repo_result.get("stack_tokens", []),
        expected_ports=label.get("expected_ports", {}),
    )

    report = {
        "repo": score.repo,
        "package_path": repo_result.get("package_path", "."),
        "error": repo_result.get("error"),
        "stack_summary": repo_result.get("stack_summary", ""),
        "stack_tokens": repo_result.get("stack_tokens", []),
        "predicted_services": [
            {
                "name": service.get("name"),
                "build_context": service.get("build_context"),
                "port": service.get("port"),
            }
            for service in sorted(
                repo_result.get("services", []),
                key=lambda svc: (
                    str(svc.get("build_context", ".")),
                    str(svc.get("name", "")),
                ),
            )
        ],
        "expected_services": [
            {"name": name, "build_context": ctx}
            for name, ctx in sorted(score.expected_services)
        ],
        "metrics": {
            "true_positives": score.true_positives,
            "false_positives": score.false_positives,
            "false_negatives": score.false_negatives,
            "leaked_mobile_services": score.leaked_mobile_services,
            "stack_match": score.stack_match,
            "known_port_count": score.known_port_count,
            "correct_port_count": score.correct_port_count,
            "missing_port_count": score.missing_port_count,
        },
    }
    report["failure_bucket"] = _failure_bucket_from_report(report)
    return report


def _failure_bucket_from_report(report: Dict[str, Any]) -> str:
    error_text = (report.get("error") or "").lower()
    metrics = report.get("metrics", {}) or {}

    if error_text:
        if "package path" in error_text and "not found" in error_text:
            return "scan_context_missing"
        if "no repository context" in error_text:
            return "planner_context_missing"
        if "no web-deployable services found" in error_text:
            return "planner_no_services"
        return "runtime_error"

    if metrics.get("stack_match") is False:
        return "stack_mismatch"

    known_port_count = int(metrics.get("known_port_count", 0) or 0)
    correct_port_count = int(metrics.get("correct_port_count", 0) or 0)
    missing_port_count = int(metrics.get("missing_port_count", 0) or 0)
    if known_port_count > 0 and correct_port_count == 0:
        if missing_port_count == known_port_count:
            return "port_missing"
        return "port_mismatch"

    false_negatives = int(metrics.get("false_negatives", 0) or 0)
    true_positives = int(metrics.get("true_positives", 0) or 0)
    if false_negatives > 0 and true_positives == 0:
        return "service_recall_miss"

    false_positives = int(metrics.get("false_positives", 0) or 0)
    if false_positives > 0:
        return "service_precision_miss"

    return "ok"


def _default_output_path() -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return os.path.join("benchmarks", f"scan-quality-{now}.json")


def run() -> int:
    parser = argparse.ArgumentParser(description="Evaluate scanner+planner accuracy against labeled repos")
    parser.add_argument("--labels-file", default=os.path.join("benchmarks", "example_bank_labels.json"))
    parser.add_argument("--github-token", default=os.getenv("GITHUB_TOKEN"))
    parser.add_argument("--max-files", type=int, default=50)
    parser.add_argument("--repos", nargs="*", default=[])
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    labels = _load_labels(args.labels_file)

    requested_repos: List[str] = [_repo_from_url_or_full_name(repo) for repo in args.repos]
    requested_repos = [repo for repo in requested_repos if repo]

    targets: List[Dict[str, Any]] = []
    for label in labels:
        if requested_repos and label["repo"] not in requested_repos:
            continue
        targets.append({
            "repo": label["repo"],
            "repo_url": label["repo_url"],
            "package_path": label.get("package_path", "."),
            "label": label,
        })

    if not targets:
        print("No evaluation targets available in the labels file. Provide a labels file with repo or repo_url entries.")
        return 1

    scored_reports: List[Dict[str, Any]] = []
    score_inputs = []
    unlabeled_targets = []
    run_errors = []

    start_time = time.perf_counter()

    for target in targets:
        repo = target["repo"]
        repo_url = target["repo_url"]
        package_path = target.get("package_path", ".")
        print(f"Evaluating {repo} (package_path={package_path})...")
        result = _run_planner_for_repo(repo, repo_url, args.github_token, args.max_files, package_path=package_path)
        label = target.get("label")

        if result.get("error"):
            run_errors.append({"repo": repo, "package_path": package_path, "error": result["error"]})

        score_obj = score_repo(
            repo=repo,
            predicted_services=result.get("services", []),
            expected_services=label.get("expected_services", []),
            excluded_services=label.get("excluded_services", []),
            required_stack_tokens=label.get("required_stack_tokens", []),
            predicted_stack=result.get("stack_summary", ""),
            predicted_stack_tokens=result.get("stack_tokens", []),
            expected_ports=label.get("expected_ports", {}),
        )
        score_inputs.append(score_obj)
        scored_reports.append(_build_repo_report(result, label))

    elapsed_seconds = time.perf_counter() - start_time

    summary = summarize_scores(score_inputs)
    summary["elapsed_seconds"] = round(elapsed_seconds, 3)
    summary["targets_total"] = len(targets)
    summary["targets_with_labels"] = len(score_inputs)
    summary["targets_without_labels"] = len(unlabeled_targets)
    summary["targets_with_runtime_errors"] = len(run_errors)
    failure_buckets: Dict[str, int] = {}
    for report in scored_reports:
        bucket = report.get("failure_bucket", "unknown")
        failure_buckets[bucket] = failure_buckets.get(bucket, 0) + 1
    summary["failure_buckets"] = failure_buckets

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "labels_file": args.labels_file,
        "targets_evaluated": [{"repo": t["repo"], "repo_url": t["repo_url"], "package_path": t.get("package_path", ".")} for t in targets],
        "summary": summary,
        "repo_reports": scored_reports,
        "targets_without_labels": unlabeled_targets,
        "runtime_errors": run_errors,
    }

    output_path = args.output or _default_output_path()
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)

    print("\nScan quality summary")
    print(json.dumps(summary, indent=2))
    print(f"\nDetailed report written to: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
