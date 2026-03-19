from typing import Dict, Any, List
from pydantic import BaseModel, Field, model_validator
import json
import re
import subprocess
from .llm_config import llm_verifier, RETRY_CONFIGS, FALLBACK_PROMPTS
from graph.llm_retry import invoke_with_retry


class VerifierOutput(BaseModel):
    confidence: float = Field(description="Confidence score from 0.0 to 1.0 on the quality of all generated artifacts")
    risks: List[str] = Field(description="List of identified risks, issues, or warnings about the generated artifacts")
    
    @model_validator(mode="before")
    @classmethod
    def coerce_risks_to_list(cls, data):
        """Handle LLM returning risks as a single string instead of a list."""
        if isinstance(data, dict) and isinstance(data.get("risks"), str):
            raw = data["risks"].strip()
            # Split on: bullet points, numbered items, or quoted-newline separators like "\n"
            items = re.split(r'"\s*\n\s*"|\\n|\\"\s*\\n\s*\\"|(?:\n\s*[-*•]\s*)|(?:\n\s*\d+\.\s*)', raw)
            # Clean up and filter empty strings
            data["risks"] = [item.strip().strip('"').strip("'") for item in items if item.strip()]
        return data


def run_hadolint(dockerfile_content: str) -> str:
    """Run hadolint on the provided Dockerfile content and return findings."""
    try:
        result = subprocess.run(
            ["hadolint", "-"],
            input=dockerfile_content,
            text=True,
            capture_output=True,
            check=False
        )
        return result.stdout.strip() if result.stdout else result.stderr.strip()
    except FileNotFoundError:
        return "hadolint not installed or not found in PATH."
    except Exception as e:
        return f"Error running hadolint: {e}"


def _looks_stateful_service(name: str) -> bool:
    lowered = (name or "").strip().lower()
    return any(token in lowered for token in ("postgres", "mysql", "mariadb", "mongo", "redis"))


def _contains_backend_healthcheck(services: List[Dict[str, Any]], dockerfiles: Dict[str, str]) -> bool:
    backend_like = {
        str(svc.get("name", "")).strip()
        for svc in services
        if isinstance(svc, dict)
        and any(token in str(svc.get("name", "")).lower() for token in ("backend", "api", "server"))
    }
    if not backend_like:
        return True
    for name in backend_like:
        content = dockerfiles.get(name, "")
        if isinstance(content, str) and "healthcheck" in content.lower():
            return True
    return False


def _filter_risks(
    risks: List[str],
    services: List[Dict[str, Any]],
    dockerfiles: Dict[str, str],
    docker_compose: str,
    nginx_conf: str,
) -> List[str]:
    """Drop generic/non-actionable verifier warnings when repo evidence contradicts them."""
    filtered: List[str] = []
    compose_lower = docker_compose.lower() if isinstance(docker_compose, str) else ""
    nginx_lower = nginx_conf.lower() if isinstance(nginx_conf, str) else ""
    compose_has_nginx_service = bool(re.search(r"(?im)^\s*nginx\s*:", compose_lower))
    has_stateful = any(
        _looks_stateful_service(str(svc.get("name", "")))
        for svc in services
        if isinstance(svc, dict)
    )
    has_backend_healthcheck = _contains_backend_healthcheck(services, dockerfiles)

    for risk in risks:
        text = str(risk or "").strip()
        lowered = text.lower()
        if not text:
            continue

        # Generic hardening nits that are not required blockers for this project.
        if "apk" in lowered and any(token in lowered for token in ("non-pinned", "not pinning", "pinning", "unpinned", "unversioned")):
            continue
        if "hadolint" in lowered and "unversioned" in lowered and "package" in lowered:
            continue
        if "base node" in lowered and "pinning" in lowered:
            continue

        if "mobile" in lowered and any(token in lowered for token in ("missing dockerfile", "missing service", "not provided")):
            has_mobile_service = any(
                isinstance(svc, dict)
                and any(token in str(svc.get("name", "")).lower() for token in ("mobile", "android", "ios", "react-native"))
                for svc in services
            )
            if not has_mobile_service:
                continue
        if "consecutive run" in lowered and "dockerfile" in lowered:
            continue

        # Only require persistent volumes when stateful services are present.
        if (
            ("persistent" in lowered or "volume" in lowered)
            and any(token in lowered for token in ("missing", "no explicit", "not defined"))
            and not has_stateful
        ):
            continue

        # Suppress healthcheck warnings when backend Dockerfile already defines HEALTHCHECK.
        if "health check" in lowered and "backend" in lowered and has_backend_healthcheck:
            continue

        # Suppress generic secret-management warning unless explicit hardcoded secrets are found.
        if "secret management" in lowered or "sensitive credentials" in lowered:
            hardcoded_secret_markers = (
                "password=",
                "passwd=",
                "api_key=",
                "secret=",
            )
            if not any(marker in compose_lower for marker in hardcoded_secret_markers):
                continue

        # Environment placeholders in compose are expected for deploy-time injection.
        if "docker-compose" in lowered and "environment variable" in lowered and "not explicitly defined" in lowered:
            continue

        # Suppress websocket hardening warnings when required proxy headers are present.
        if "websocket" in lowered and "nginx" in lowered and "security" in lowered:
            has_ws_route = "location /ws" in nginx_lower
            has_upgrade = "proxy_set_header upgrade" in nginx_lower
            has_connection = "proxy_set_header connection" in nginx_lower
            has_forwarded = "x-forwarded-for" in nginx_lower
            if has_ws_route and has_upgrade and has_connection and has_forwarded:
                continue

        # In smart-deploy's current flow nginx runs on host OS, so localhost upstreams are expected.
        if "nginx" in lowered and "localhost" in lowered and "container" in lowered and not compose_has_nginx_service:
            continue

        filtered.append(text)

    return filtered


def _compute_deterministic_confidence(
    services: List[Dict[str, Any]],
    dockerfiles: Dict[str, str],
    docker_compose: str,
    nginx_conf: str,
    risks: List[str],
) -> float:
    """Compute confidence from final artifacts and filtered risks without LLM score dependence."""
    service_list = [svc for svc in services if isinstance(svc, dict)]
    service_count = len(service_list)

    score = 1.0

    if service_count == 0:
        score -= 0.6

    missing_dockerfiles = 0
    missing_ports = 0
    for svc in service_list:
        name = str(svc.get("name", "")).strip()
        if name and not isinstance(dockerfiles.get(name), str):
            missing_dockerfiles += 1
        try:
            port = int(svc.get("port"))
            if port <= 0:
                missing_ports += 1
        except (TypeError, ValueError):
            missing_ports += 1

    if missing_dockerfiles:
        score -= min(0.3, 0.15 * missing_dockerfiles)
    if missing_ports:
        score -= min(0.15, 0.05 * missing_ports)

    # Multi-service deployments should include compose.
    if service_count > 1 and not (isinstance(docker_compose, str) and docker_compose.strip()):
        score -= 0.2

    # If frontend + backend style services are present, nginx should be generated.
    has_frontend = any(
        any(token in str(svc.get("name", "")).lower() for token in ("web", "frontend", "ui", "client", "next"))
        for svc in service_list
    )
    has_backend = any(
        any(token in str(svc.get("name", "")).lower() for token in ("backend", "api", "server"))
        for svc in service_list
    )
    if has_frontend and has_backend and not (isinstance(nginx_conf, str) and nginx_conf.strip()):
        score -= 0.1

    # Final filtered risks directly reduce confidence.
    score -= min(0.6, 0.12 * len(risks))

    # Keep confidence in a practical range and stable for API consumers.
    bounded = max(0.1, min(0.99, score))
    return round(bounded, 2)

def verifier_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Review all generated artifacts and assign confidence + risks."""
    scan = state.get("repo_scan", {})
    services = state.get("services", [])
    dockerfiles = state.get("dockerfiles", {})
    docker_compose = state.get("docker_compose", "")
    nginx_conf = state.get("nginx_conf", "")

    hadolint_results = {}
    for service, content in dockerfiles.items():
        hadolint_results[service] = run_hadolint(content)
        
    hadolint_output_str = json.dumps(hadolint_results, indent=2)

    prompt = f"""
You are a senior DevOps reviewer. Review ALL the generated deployment artifacts below for a repository and assess their quality.

REPO INFO:
- Stack: {state.get('detected_stack', 'unknown')}
- Services: {json.dumps(services, indent=2)}
- Key files found: {list(scan.get('key_files', {}).keys())}
- Directories: {scan.get('dirs', [])}

GENERATED DOCKERFILES:
{json.dumps(dockerfiles, indent=2)}

HADOLINT ANALYSIS (LINTER RESULTS):
{hadolint_output_str}

GENERATED DOCKER-COMPOSE:
{docker_compose}

GENERATED NGINX CONFIG:
{nginx_conf}

Review for:
1. Port consistency — do Dockerfiles EXPOSE the same ports referenced in compose and nginx?
2. Build context accuracy — do compose build contexts match the actual repo directory structure?
3. Security — non-root users, no hardcoded secrets, proper headers in nginx?
4. Completeness — are all services accounted for? Are missing env vars flagged?
5. Best practices — multi-stage builds, health checks, proper caching layers?

Provide a confidence score (0.0 to 1.0) and a list of specific risks or issues found.
Each risk must be a separate string in the list. Do NOT combine multiple risks into one string.
Do NOT include generic advice-only risks unless there is concrete evidence in the artifacts.
If everything looks good, confidence should be high (0.85+) with an empty or minimal risks list.
"""

    try:
        def _invoke(raw_prompt: str):
            structured_llm = llm_verifier.with_structured_output(VerifierOutput)
            return structured_llm.invoke(raw_prompt)

        result, attempts_used, fallback_used = invoke_with_retry(
            invoke_fn=_invoke,
            prompt=prompt,
            fallback_prompt=FALLBACK_PROMPTS["verifier"],
            config=RETRY_CONFIGS["verifier"],
            node_name="verifier",
        )
        filtered_risks = _filter_risks(result.risks, services, dockerfiles, docker_compose, nginx_conf)
        state["risks"] = filtered_risks
        state["confidence"] = _compute_deterministic_confidence(
            services=services,
            dockerfiles=dockerfiles,
            docker_compose=docker_compose,
            nginx_conf=nginx_conf,
            risks=filtered_risks,
        )
        state["llm_confidence_raw"] = result.confidence
        state["hadolint_results"] = hadolint_results
        state["verifier_retry_attempts"] = attempts_used
        state["verifier_fallback_used"] = fallback_used
    except Exception as e:
        state["confidence"] = 0.5
        state["risks"] = [f"Verifier failed to run: {e}"]
        state["hadolint_results"] = hadolint_results
    
    return state
