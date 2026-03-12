from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import yaml

from tools.stack_tokens import normalize_stack_tokens


ServiceKey = Tuple[str, str]
ContextKey = str

ARTIFACT_SCORE_SCHEMA_VERSION = "v1"

# Weights are normalized per artifact and used as the canonical contract for V2 scoring.
ARTIFACT_SCORE_WEIGHTS: Dict[str, Dict[str, float]] = {
    "dockerfile": {
        "base_image": 0.225,
        "non_root_user": 0.225,
        "expose_or_documented_port": 0.225,
        "healthcheck": 0.10,
        "stack_alignment": 0.225,
    },
    "compose": {
        "service_coverage": 0.25,
        "build_context_validity": 0.20,
        "port_mappings": 0.20,
        "env_placeholders": 0.15,
        "volume_hygiene": 0.10,
        "syntax_validity": 0.10,
    },
    "nginx": {
        "route_coverage": 0.30,
        "proxy_correctness": 0.25,
        "security_headers": 0.20,
        "websocket_handling": 0.15,
        "syntax_sanity": 0.10,
    },
}

ARTIFACT_PASS_THRESHOLDS: Dict[str, float] = {
    "dockerfile": 0.90,
    "compose": 0.90,
    "nginx": 0.85,
}


@dataclass
class RepoScore:
    repo: str
    predicted_services: Set[ServiceKey]
    expected_services: Set[ServiceKey]
    excluded_services: Set[ServiceKey]
    predicted_contexts: Set[ContextKey]
    expected_contexts: Set[ContextKey]
    excluded_contexts: Set[ContextKey]
    true_positives: int
    false_positives: int
    false_negatives: int
    leaked_mobile_services: int
    predicted_count: int
    stack_match: Optional[bool]
    known_port_count: int
    correct_port_count: int
    missing_port_count: int


def normalize_path(path: str) -> str:
    normalized = (path or ".").replace("\\", "/").strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized or "."


def normalize_name(name: str) -> str:
    return (name or "").strip().lower()


def make_service_key(name: str, build_context: str) -> ServiceKey:
    return (normalize_name(name), normalize_path(build_context))


def service_keys(services: Iterable[Dict]) -> Set[ServiceKey]:
    keys: Set[ServiceKey] = set()
    for service in services or []:
        if not isinstance(service, dict):
            continue
        keys.add(make_service_key(service.get("name", ""), service.get("build_context", ".")))
    return keys


def service_contexts(services: Iterable[Dict]) -> Set[ContextKey]:
    contexts: Set[ContextKey] = set()
    for service in services or []:
        if not isinstance(service, dict):
            continue
        contexts.add(normalize_path(service.get("build_context", ".")))
    return contexts


def _stack_matches(predicted_stack: str, required_tokens: List[str], predicted_stack_tokens: Optional[List[str]] = None) -> Optional[bool]:
    if not required_tokens:
        return None
    required = set(normalize_stack_tokens(required_tokens))
    if predicted_stack_tokens is not None:
        predicted = set(normalize_stack_tokens(predicted_stack_tokens))
        return required.issubset(predicted)
    haystack = (predicted_stack or "").lower()
    return all((token or "").lower() in haystack for token in required)


def _predicted_port_map(predicted_services: Iterable[Dict]) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for service in predicted_services or []:
        if not isinstance(service, dict):
            continue
        name = normalize_name(service.get("name", ""))
        port = service.get("port")
        if name and isinstance(port, int):
            result[name] = port
    return result


def _predicted_port_map_by_context(predicted_services: Iterable[Dict]) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for service in predicted_services or []:
        if not isinstance(service, dict):
            continue
        build_context = normalize_path(service.get("build_context", "."))
        port = service.get("port")
        if build_context and isinstance(port, int):
            result[build_context] = port
    return result


def score_repo(
    repo: str,
    predicted_services: List[Dict],
    expected_services: List[Dict],
    excluded_services: Optional[List[Dict]] = None,
    required_stack_tokens: Optional[List[str]] = None,
    predicted_stack: str = "",
    predicted_stack_tokens: Optional[List[str]] = None,
    expected_ports: Optional[Dict[str, int]] = None,
) -> RepoScore:
    predicted_keys = service_keys(predicted_services)
    expected_keys = service_keys(expected_services)
    excluded_keys = service_keys(excluded_services or [])

    predicted_contexts = service_contexts(predicted_services)
    expected_contexts = service_contexts(expected_services)
    excluded_contexts = service_contexts(excluded_services or [])

    tp = len(predicted_contexts & expected_contexts)
    fp = len(predicted_contexts - expected_contexts)
    fn = len(expected_contexts - predicted_contexts)
    leaked = len(predicted_contexts & excluded_contexts)

    predicted_port_map = _predicted_port_map(predicted_services)
    predicted_port_map_by_context = _predicted_port_map_by_context(predicted_services)
    known_port_count = 0
    correct_port_count = 0
    missing_port_count = 0

    expected_context_by_name = {
        normalize_name(service.get("name", "")): normalize_path(service.get("build_context", "."))
        for service in expected_services or []
        if isinstance(service, dict)
    }

    for service_name, expected_port in (expected_ports or {}).items():
        norm_name = normalize_name(service_name)
        if not norm_name:
            continue
        known_port_count += 1
        predicted_port = predicted_port_map.get(norm_name)
        if predicted_port is None:
            expected_context = expected_context_by_name.get(norm_name)
            if expected_context:
                predicted_port = predicted_port_map_by_context.get(expected_context)
        if predicted_port is None:
            missing_port_count += 1
        elif predicted_port == expected_port:
            correct_port_count += 1

    stack_match = _stack_matches(predicted_stack, required_stack_tokens or [], predicted_stack_tokens)

    return RepoScore(
        repo=repo,
        predicted_services=predicted_keys,
        expected_services=expected_keys,
        excluded_services=excluded_keys,
        predicted_contexts=predicted_contexts,
        expected_contexts=expected_contexts,
        excluded_contexts=excluded_contexts,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        leaked_mobile_services=leaked,
        predicted_count=len(predicted_keys),
        stack_match=stack_match,
        known_port_count=known_port_count,
        correct_port_count=correct_port_count,
        missing_port_count=missing_port_count,
    )


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def artifact_scoring_contract() -> Dict[str, Any]:
    return {
        "artifact_score_schema_version": ARTIFACT_SCORE_SCHEMA_VERSION,
        "weights": ARTIFACT_SCORE_WEIGHTS,
        "thresholds": ARTIFACT_PASS_THRESHOLDS,
    }


@dataclass
class DockerfileScore:
    total_score: float
    criteria_scores: Dict[str, float]
    criterion_reasons: Dict[str, str]
    passed_threshold: bool


@dataclass
class ComposeScore:
    total_score: float
    criteria_scores: Dict[str, float]
    criterion_reasons: Dict[str, str]
    passed_threshold: bool


@dataclass
class NginxScore:
    total_score: float
    criteria_scores: Dict[str, float]
    criterion_reasons: Dict[str, str]
    passed_threshold: bool


def _parse_dockerfile_lines(content: str) -> List[str]:
    """Parse Dockerfile content into non-comment, stripped lines."""
    lines = []
    for line in (content or "").split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped.upper())
    return lines


def _dockerfile_has_base_image(lines: List[str]) -> bool:
    """Check for FROM instruction."""
    return any(line.startswith("FROM") for line in lines)


def _dockerfile_has_non_root_user(lines: List[str]) -> bool:
    """Check for USER instruction with non-root user."""
    for line in lines:
        if line.startswith("USER"):
            parts = line.split()
            if len(parts) >= 2:
                user = parts[1]
                if user not in ("0", "ROOT"):
                    return True
    return False


def _dockerfile_has_expose_or_port(lines: List[str]) -> bool:
    """Check for EXPOSE instruction or documented port in comments."""
    return any(
        line.startswith("EXPOSE")
        for line in lines
    )


def _dockerfile_has_healthcheck(lines: List[str]) -> bool:
    """Check for HEALTHCHECK instruction."""
    return any(line.startswith("HEALTHCHECK") for line in lines)


def _dockerfile_stack_alignment(lines: List[str], required_tokens: Optional[List[str]] = None) -> bool:
    """Check if key stack markers appear in the Dockerfile."""
    if not required_tokens:
        return True
    required_lower = {(token or "").lower() for token in required_tokens}
    content_lower = "\n".join(lines).lower()
    return all(token in content_lower for token in required_lower if token)


def score_dockerfile(
    content: str,
    required_stack_tokens: Optional[List[str]] = None,
) -> DockerfileScore:
    """
    Score a Dockerfile against weighted criteria.
    Returns criterion-level scores and explainable reasons.
    """
    if not content or not content.strip():
        return DockerfileScore(
            total_score=0.0,
            criteria_scores={
                "base_image": 0.0,
                "non_root_user": 0.0,
                "expose_or_documented_port": 0.0,
                "healthcheck": 0.0,
                "stack_alignment": 0.0,
            },
            criterion_reasons={
                "base_image": "Empty or missing Dockerfile",
                "non_root_user": "Empty or missing Dockerfile",
                "expose_or_documented_port": "Empty or missing Dockerfile",
                "healthcheck": "Empty or missing Dockerfile",
                "stack_alignment": "Empty or missing Dockerfile",
            },
            passed_threshold=False,
        )

    lines = _parse_dockerfile_lines(content)
    weights = ARTIFACT_SCORE_WEIGHTS["dockerfile"]

    criteria_scores: Dict[str, float] = {}
    reasons: Dict[str, str] = {}

    # Criterion 1: base_image
    has_base = _dockerfile_has_base_image(lines)
    criteria_scores["base_image"] = 1.0 if has_base else 0.0
    reasons["base_image"] = "FROM instruction found" if has_base else "No FROM instruction found"

    # Criterion 2: non_root_user
    has_non_root = _dockerfile_has_non_root_user(lines)
    criteria_scores["non_root_user"] = 1.0 if has_non_root else 0.0
    reasons["non_root_user"] = "Non-root USER found" if has_non_root else "No non-root USER instruction"

    # Criterion 3: expose_or_documented_port
    has_expose = _dockerfile_has_expose_or_port(lines)
    criteria_scores["expose_or_documented_port"] = 1.0 if has_expose else 0.0
    reasons["expose_or_documented_port"] = "EXPOSE instruction or port documentation found" if has_expose else "No EXPOSE or port documentation"

    # Criterion 4: healthcheck
    has_healthcheck = _dockerfile_has_healthcheck(lines)
    criteria_scores["healthcheck"] = 1.0 if has_healthcheck else 0.0
    reasons["healthcheck"] = "HEALTHCHECK instruction found" if has_healthcheck else "No HEALTHCHECK instruction"

    # Criterion 5: stack_alignment
    stack_aligned = _dockerfile_stack_alignment(lines, required_stack_tokens)
    criteria_scores["stack_alignment"] = 1.0 if stack_aligned else 0.0
    reasons["stack_alignment"] = f"Stack tokens {required_stack_tokens} found in Dockerfile" if stack_aligned else f"Missing stack tokens {required_stack_tokens}"

    # Compute total weighted score
    total = sum(criteria_scores[key] * weights[key] for key in weights.keys())
    threshold = ARTIFACT_PASS_THRESHOLDS["dockerfile"]

    return DockerfileScore(
        total_score=total,
        criteria_scores=criteria_scores,
        criterion_reasons=reasons,
        passed_threshold=total >= threshold,
    )


def _empty_compose_score(message: str) -> ComposeScore:
    criteria = {
        "service_coverage": 0.0,
        "build_context_validity": 0.0,
        "port_mappings": 0.0,
        "env_placeholders": 0.0,
        "volume_hygiene": 0.0,
        "syntax_validity": 0.0,
    }
    reasons = {key: message for key in criteria.keys()}
    return ComposeScore(
        total_score=0.0,
        criteria_scores=criteria,
        criterion_reasons=reasons,
        passed_threshold=False,
    )


def score_compose(
    content: str,
    expected_services: Optional[List[Dict[str, Any]]] = None,
) -> ComposeScore:
    if not content or not content.strip():
        return _empty_compose_score("Empty or missing compose file")

    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        score = _empty_compose_score("Invalid compose YAML syntax")
        score.criterion_reasons["syntax_validity"] = f"Invalid compose YAML syntax: {exc.__class__.__name__}"
        return score

    if not isinstance(parsed, dict):
        return _empty_compose_score("Compose root must be a YAML object")

    services_raw = parsed.get("services")
    if not isinstance(services_raw, dict):
        return _empty_compose_score("Missing or invalid services section")

    weights = ARTIFACT_SCORE_WEIGHTS["compose"]
    criteria_scores: Dict[str, float] = {}
    reasons: Dict[str, str] = {}

    criteria_scores["syntax_validity"] = 1.0
    reasons["syntax_validity"] = "Compose YAML parsed successfully"

    expected_map: Dict[str, str] = {}
    for expected in expected_services or []:
        if not isinstance(expected, dict):
            continue
        name = normalize_name(str(expected.get("name", "")))
        context = normalize_path(str(expected.get("build_context", ".")))
        if name:
            expected_map[name] = context

    service_names = [normalize_name(name) for name in services_raw.keys()]
    normalized_service_names = {name for name in service_names if name}

    if expected_map:
        covered = len(set(expected_map.keys()) & normalized_service_names)
        total_expected = len(expected_map)
        coverage = covered / total_expected if total_expected else 1.0
        missing = sorted(set(expected_map.keys()) - normalized_service_names)
        criteria_scores["service_coverage"] = round(coverage, 6)
        if missing:
            reasons["service_coverage"] = f"Missing expected services: {', '.join(missing)}"
        else:
            reasons["service_coverage"] = "All expected services are present"
    else:
        criteria_scores["service_coverage"] = 1.0 if normalized_service_names else 0.0
        reasons["service_coverage"] = (
            "Service entries found" if normalized_service_names else "No services defined"
        )

    build_defined = 0
    build_valid = 0
    for raw_name, cfg in services_raw.items():
        if not isinstance(cfg, dict):
            continue
        name = normalize_name(str(raw_name))
        if "build" not in cfg:
            continue
        build_defined += 1
        build_value = cfg.get("build")
        actual_context: Optional[str] = None
        if isinstance(build_value, str):
            actual_context = normalize_path(build_value)
        elif isinstance(build_value, dict):
            context_value = build_value.get("context")
            if isinstance(context_value, str) and context_value.strip():
                actual_context = normalize_path(context_value)

        if actual_context is None:
            continue
        if name in expected_map:
            if normalize_path(expected_map[name]) == actual_context:
                build_valid += 1
        else:
            build_valid += 1

    if build_defined == 0:
        criteria_scores["build_context_validity"] = 0.0
        reasons["build_context_validity"] = "No build contexts found"
    else:
        build_ratio = build_valid / build_defined
        criteria_scores["build_context_validity"] = round(build_ratio, 6)
        if build_ratio == 1.0:
            reasons["build_context_validity"] = "All build contexts are valid"
        else:
            reasons["build_context_validity"] = "One or more build contexts are invalid or mismatched"

    services_with_ports = 0
    for cfg in services_raw.values():
        if not isinstance(cfg, dict):
            continue
        ports = cfg.get("ports")
        if isinstance(ports, list) and len(ports) > 0:
            services_with_ports += 1
    total_services = len([cfg for cfg in services_raw.values() if isinstance(cfg, dict)])
    if total_services == 0:
        criteria_scores["port_mappings"] = 0.0
        reasons["port_mappings"] = "No valid services available for port checks"
    else:
        port_ratio = services_with_ports / total_services
        criteria_scores["port_mappings"] = round(port_ratio, 6)
        reasons["port_mappings"] = (
            "All services declare ports" if port_ratio == 1.0 else "Some services are missing ports"
        )

    env_configured = 0
    env_total = 0
    for cfg in services_raw.values():
        if not isinstance(cfg, dict):
            continue
        if "environment" in cfg:
            env_total += 1
            env_value = cfg.get("environment")
            if isinstance(env_value, dict):
                env_configured += 1
            elif isinstance(env_value, list) and env_value:
                env_configured += 1
    if env_total == 0:
        criteria_scores["env_placeholders"] = 1.0
        reasons["env_placeholders"] = "No environment placeholders required"
    else:
        env_ratio = env_configured / env_total
        criteria_scores["env_placeholders"] = round(env_ratio, 6)
        reasons["env_placeholders"] = (
            "Environment placeholders are present" if env_ratio == 1.0 else "Some environment blocks are malformed"
        )

    volume_valid = 0
    volume_total = 0
    for cfg in services_raw.values():
        if not isinstance(cfg, dict):
            continue
        volumes = cfg.get("volumes")
        if volumes is None:
            continue
        if isinstance(volumes, list):
            volume_total += 1
            if all(isinstance(v, (str, dict)) for v in volumes):
                volume_valid += 1
        else:
            volume_total += 1
    if volume_total == 0:
        criteria_scores["volume_hygiene"] = 1.0
        reasons["volume_hygiene"] = "No volumes declared"
    else:
        volume_ratio = volume_valid / volume_total
        criteria_scores["volume_hygiene"] = round(volume_ratio, 6)
        reasons["volume_hygiene"] = (
            "Volume declarations are valid" if volume_ratio == 1.0 else "One or more volume declarations are invalid"
        )

    total = sum(criteria_scores[key] * weights[key] for key in weights.keys())
    threshold = ARTIFACT_PASS_THRESHOLDS["compose"]

    return ComposeScore(
        total_score=round(total, 6),
        criteria_scores=criteria_scores,
        criterion_reasons=reasons,
        passed_threshold=total >= threshold,
    )


def _empty_nginx_score(message: str) -> NginxScore:
    criteria = {
        "route_coverage": 0.0,
        "proxy_correctness": 0.0,
        "security_headers": 0.0,
        "websocket_handling": 0.0,
        "syntax_sanity": 0.0,
    }
    reasons = {key: message for key in criteria.keys()}
    return NginxScore(
        total_score=0.0,
        criteria_scores=criteria,
        criterion_reasons=reasons,
        passed_threshold=False,
    )


def _nginx_lines(content: str) -> List[str]:
    lines: List[str] = []
    for raw in (content or "").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


def _extract_proxy_pass_targets(lines: List[str]) -> List[str]:
    targets: List[str] = []
    for line in lines:
        lower = line.lower()
        if not lower.startswith("proxy_pass"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        target = parts[1].rstrip(";")
        targets.append(target)
    return targets


def _has_balanced_braces(content: str) -> bool:
    balance = 0
    for char in content:
        if char == "{":
            balance += 1
        elif char == "}":
            balance -= 1
            if balance < 0:
                return False
    return balance == 0


def score_nginx(
    content: str,
    expected_services: Optional[List[Dict[str, Any]]] = None,
) -> NginxScore:
    if not content or not content.strip():
        return _empty_nginx_score("Empty or missing nginx config")

    lines = _nginx_lines(content)
    if not lines:
        return _empty_nginx_score("Empty or missing nginx config")

    weights = ARTIFACT_SCORE_WEIGHTS["nginx"]
    criteria_scores: Dict[str, float] = {}
    reasons: Dict[str, str] = {}

    expected_names: Set[str] = set()
    for service in expected_services or []:
        if not isinstance(service, dict):
            continue
        name = normalize_name(str(service.get("name", "")))
        if name:
            expected_names.add(name)

    lower_content = "\n".join(lines).lower()
    proxy_targets = _extract_proxy_pass_targets(lines)
    lower_proxy_targets = [target.lower() for target in proxy_targets]

    if expected_names:
        covered = 0
        for name in expected_names:
            if any(name in target for target in lower_proxy_targets) or name in lower_content:
                covered += 1
        coverage = covered / len(expected_names)
        criteria_scores["route_coverage"] = round(coverage, 6)
        if coverage == 1.0:
            reasons["route_coverage"] = "All expected services have nginx route coverage"
        else:
            reasons["route_coverage"] = "Some expected services are missing route coverage"
    else:
        has_proxy = len(proxy_targets) > 0
        criteria_scores["route_coverage"] = 1.0 if has_proxy else 0.0
        reasons["route_coverage"] = "Proxy routes detected" if has_proxy else "No proxy routes detected"

    if not proxy_targets:
        criteria_scores["proxy_correctness"] = 0.0
        reasons["proxy_correctness"] = "No proxy_pass directives found"
    else:
        valid_targets = 0
        for target in proxy_targets:
            normalized = target.lower()
            if normalized.startswith("http://") or normalized.startswith("https://"):
                valid_targets += 1
            elif normalized.startswith("unix:"):
                valid_targets += 1
        proxy_ratio = valid_targets / len(proxy_targets)
        criteria_scores["proxy_correctness"] = round(proxy_ratio, 6)
        reasons["proxy_correctness"] = (
            "proxy_pass targets look valid" if proxy_ratio == 1.0 else "Some proxy_pass targets look malformed"
        )

    required_headers = [
        "x-content-type-options",
        "x-frame-options",
        "content-security-policy",
    ]
    found_headers = sum(1 for header in required_headers if header in lower_content)
    header_ratio = found_headers / len(required_headers)
    criteria_scores["security_headers"] = round(header_ratio, 6)
    reasons["security_headers"] = (
        "Security headers are configured"
        if header_ratio == 1.0
        else "One or more recommended security headers are missing"
    )

    has_upgrade = "upgrade" in lower_content
    has_connection_upgrade = 'connection "upgrade"' in lower_content or "connection upgrade" in lower_content
    has_http11 = "proxy_http_version 1.1" in lower_content
    ws_parts = int(has_upgrade) + int(has_connection_upgrade) + int(has_http11)
    websocket_ratio = ws_parts / 3
    criteria_scores["websocket_handling"] = round(websocket_ratio, 6)
    reasons["websocket_handling"] = (
        "Websocket proxy settings detected"
        if websocket_ratio == 1.0
        else "Websocket proxy settings are partial or missing"
    )

    has_server_block = "server {" in lower_content or "server{" in lower_content
    has_events_or_http = "events {" in lower_content or "http {" in lower_content
    braces_ok = _has_balanced_braces(content)
    syntax_checks = [has_server_block, has_events_or_http, braces_ok]
    syntax_ratio = sum(1 for check in syntax_checks if check) / len(syntax_checks)
    criteria_scores["syntax_sanity"] = round(syntax_ratio, 6)
    reasons["syntax_sanity"] = (
        "Config structure looks syntactically sane"
        if syntax_ratio == 1.0
        else "Config structure may be incomplete or malformed"
    )

    total = sum(criteria_scores[key] * weights[key] for key in weights.keys())
    threshold = ARTIFACT_PASS_THRESHOLDS["nginx"]

    return NginxScore(
        total_score=round(total, 6),
        criteria_scores=criteria_scores,
        criterion_reasons=reasons,
        passed_threshold=total >= threshold,
    )


def summarize_scores(scores: List[RepoScore]) -> Dict[str, float | int]:
    tp = sum(score.true_positives for score in scores)
    fp = sum(score.false_positives for score in scores)
    fn = sum(score.false_negatives for score in scores)

    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)
    f1 = 0.0
    if precision + recall > 0:
        f1 = 2 * precision * recall / (precision + recall)

    leaked = sum(score.leaked_mobile_services for score in scores)
    predicted_total = sum(score.predicted_count for score in scores)
    mobile_leakage_rate = _safe_ratio(leaked, predicted_total)

    stack_labeled = [score.stack_match for score in scores if score.stack_match is not None]
    stack_accuracy = 0.0
    if stack_labeled:
        stack_accuracy = sum(1 for matched in stack_labeled if matched) / len(stack_labeled)

    known_port_count = sum(score.known_port_count for score in scores)
    correct_port_count = sum(score.correct_port_count for score in scores)
    missing_port_count = sum(score.missing_port_count for score in scores)
    port_accuracy_known = _safe_ratio(correct_port_count, known_port_count)
    port_unknown_rate = _safe_ratio(missing_port_count, known_port_count)

    return {
        "repos_scored": len(scores),
        "service_true_positives": tp,
        "service_false_positives": fp,
        "service_false_negatives": fn,
        "service_precision": precision,
        "service_recall": recall,
        "service_f1": f1,
        "mobile_leakage_rate": mobile_leakage_rate,
        "stack_accuracy": stack_accuracy,
        "stack_labeled_repo_count": len(stack_labeled),
        "known_port_count": known_port_count,
        "correct_port_count": correct_port_count,
        "port_accuracy_known": port_accuracy_known,
        "port_unknown_rate": port_unknown_rate,
    }
