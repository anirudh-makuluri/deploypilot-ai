from typing import Any, Dict, List, Literal
import json

from langgraph.graph import StateGraph, END
from pydantic import BaseModel, Field
from langchain_core.runnables.config import RunnableConfig

from .llm_retry import invoke_with_retry
from .nodes.llm_config import (
    llm_coordinator,
    llm_docker,
    llm_compose,
    llm_nginx,
    llm_verifier,
    strip_markdown_wrapper,
    RETRY_CONFIGS,
    FALLBACK_PROMPTS,
)
from .nodes.verifier import VerifierOutput, run_hadolint
from .nodes.verifier import _filter_risks, _compute_deterministic_confidence
from .nodes.dockerfile_generator import _repair_dockerfile_output
from .nodes.compose_generator import _repair_compose_output
from .nodes.nginx_generator import _repair_nginx_output, _infer_route_flags
from .nodes.preflight import preflight_node


def _get_dockerfile_path(build_context: str) -> str:
    """Generate the dockerfile path from build context.
    
    Examples:
    - "." -> "Dockerfile"
    - "" -> "Dockerfile"
    - "client" -> "client/Dockerfile"
    - "./client" -> "client/Dockerfile"
    """
    normalized = (build_context or ".").replace("\\", "/").strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized or "."
    if normalized in (".", ""):
        return "Dockerfile"
    return f"{normalized}/Dockerfile"


class ChangeInstruction(BaseModel):
    artifact_type: Literal["dockerfile", "compose", "nginx"]
    service_name: str = ""
    should_change: bool
    instructions: str = Field(default="")


class CoordinatorOutput(BaseModel):
    change_plan: List[ChangeInstruction]
    summary: str = ""


def _default_plan(state: Dict[str, Any], reason: str) -> List[ChangeInstruction]:
    feedback = state.get("feedback", "")
    dockerfiles = state.get("dockerfiles", {})
    plan: List[ChangeInstruction] = []

    for dockerfile_path in dockerfiles.keys():
        plan.append(
            ChangeInstruction(
                artifact_type="dockerfile",
                service_name=dockerfile_path,
                should_change=True,
                instructions=f"Coordinator fallback ({reason}): Apply feedback safely. Feedback: {feedback}",
            )
        )

    services = state.get("services", [])
    if isinstance(services, list) and len(services) > 1:
        plan.append(
            ChangeInstruction(
                artifact_type="compose",
                service_name="",
                should_change=True,
                instructions=f"Coordinator fallback ({reason}): Apply feedback safely. Feedback: {feedback}",
            )
        )
    plan.append(
        ChangeInstruction(
            artifact_type="nginx",
            service_name="",
            should_change=True,
            instructions=f"Coordinator fallback ({reason}): Apply feedback safely. Feedback: {feedback}",
        )
    )
    return plan


def _get_instruction(
    plan: List[ChangeInstruction],
    artifact_type: str,
    service_name: str = "",
) -> ChangeInstruction:
    for item in plan:
        if item.artifact_type != artifact_type:
            continue
        if artifact_type == "dockerfile" and item.service_name == service_name:
            return item
        if artifact_type in {"compose", "nginx"}:
            return item
    return ChangeInstruction(
        artifact_type=artifact_type, service_name=service_name, should_change=False, instructions=""
    )


def feedback_coordinator_node(state: Dict[str, Any], config: RunnableConfig = None) -> Dict[str, Any]:
    services = state.get("services", [])
    detected_stack = state.get("detected_stack", "unknown")
    dockerfiles = state.get("dockerfiles", {})
    docker_compose = state.get("docker_compose") or ""
    nginx_conf = state.get("nginx_conf") or ""
    prior_risks = state.get("prior_risks", [])
    prior_hadolint = state.get("prior_hadolint_results", {})
    feedback = state.get("feedback", "")
    deployment_failure_summary = state.get("deployment_failure_summary", "") or ""
    deployment_failure_logs = state.get("deployment_failure_logs", "") or ""
    failed_artifact_scope = state.get("failed_artifact_scope", "") or ""
    feedback_history = state.get("prior_feedbacks", [])
    
    # Simple regression heuristic: if the exact same feedback was already submitted twice,
    # or the user has hit the loop guard, restrict changes.
    feedback_round = state.get("feedback_round", 1)
    if feedback_round >= 4:
        state["change_plan"] = _default_plan(state, "Max feedback iterations reached. Generating safe fallback.")
        state["coordinator_summary"] = "Max iterations reached. Falling back."
        return state

    history_context = ""
    if feedback_history:
        history_context = "PREVIOUS FEEDBACK ROUNDS:\n" + "\n".join(f"- Round {i+1}: {fb}" for i, fb in enumerate(feedback_history)) + "\n\nCRITICAL: Do NOT undo previous fixes. Address the NEW feedback while keeping old fixes intact."

    prompt = f"""
You are a coordinator agent for deployment artifact remediation.

Your task:
- Read user feedback + prior hadolint warnings + prior risks.
- Decide EXACTLY which artifacts need changes.
- Emit targeted instructions for the specialized file improver agents.

REPO INFO:
- Stack: {detected_stack}
- Services: {json.dumps(services, indent=2)}

USER FEEDBACK:
{feedback}

PRIOR HADOLINT WARNINGS:
{json.dumps(prior_hadolint, indent=2)}

PRIOR RISKS:
{json.dumps(prior_risks, indent=2)}

DEPLOYMENT FAILURE SUMMARY:
{deployment_failure_summary}

FAILED ARTIFACT SCOPE (if known):
{failed_artifact_scope}

DEPLOYMENT FAILURE LOGS:
{deployment_failure_logs}

CURRENT DOCKERFILES:
{json.dumps(dockerfiles, indent=2)}

CURRENT DOCKER-COMPOSE:
{docker_compose}

CURRENT NGINX:
{nginx_conf}

{history_context}

Return a structured plan. For each dockerfile in CURRENT DOCKERFILES:
- Output one instruction with artifact_type="dockerfile" and service_name set to the dockerfile path (e.g. "client/Dockerfile" or "Dockerfile")
- Set should_change=false when a file does not need modification.
Also output one instruction each for compose and nginx. (If the repo only has ONE service in SERVICES, set should_change=false for compose).
Keep instructions concise, actionable, and file-specific.
"""

    try:
        def _invoke(raw_prompt: str):
            structured = llm_coordinator.with_structured_output(CoordinatorOutput)
            try:
                return structured.invoke(raw_prompt, config=config)
            except TypeError:
                return structured.invoke(raw_prompt)

        result, _, _ = invoke_with_retry(
            invoke_fn=_invoke,
            prompt=prompt,
            fallback_prompt=FALLBACK_PROMPTS["coordinator"],
            config=RETRY_CONFIGS["coordinator"],
            node_name="feedback_coordinator",
        )
        state["change_plan"] = result.change_plan
        state["coordinator_summary"] = result.summary
    except Exception as e:
        state["change_plan"] = _default_plan(state, str(e))
        state["coordinator_summary"] = f"Coordinator fallback used due to: {e}"

    return state


def dockerfile_improver_node(state: Dict[str, Any], config: RunnableConfig = None) -> Dict[str, Any]:
    dockerfiles = state.get("dockerfiles", {})
    detected_stack = state.get("detected_stack", "unknown")
    feedback = state.get("feedback", "")
    plan = state.get("change_plan", [])
    scan = state.get("repo_scan", {})
    services = state.get("services", [])
    updated: Dict[str, str] = {}

    service_by_dockerfile: Dict[str, Dict[str, Any]] = {}
    service_by_name: Dict[str, Dict[str, Any]] = {}
    if isinstance(services, list):
        for svc in services:
            if not isinstance(svc, dict):
                continue
            name = str(svc.get("name", "")).strip()
            if name:
                service_by_name[name] = svc
            df_path = str(svc.get("dockerfile_path") or _get_dockerfile_path(str(svc.get("build_context", "."))))
            service_by_dockerfile.setdefault(df_path, svc)

    for svc_name, current_dockerfile in dockerfiles.items():
        instruction = _get_instruction(plan, artifact_type="dockerfile", service_name=svc_name)
        if not instruction.should_change:
            mapped_service = service_by_dockerfile.get(svc_name)
            mapped_name = str(mapped_service.get("name", "")).strip() if isinstance(mapped_service, dict) else ""
            if not mapped_name and svc_name in service_by_name:
                mapped_name = svc_name
            if not mapped_name and "dockerfile" in svc_name.lower():
                remaining = [
                    n for n in service_by_name.keys()
                    if n not in dockerfiles
                ]
                if len(remaining) == 1:
                    mapped_name = remaining[0]
            if mapped_name:
                instruction = _get_instruction(plan, artifact_type="dockerfile", service_name=mapped_name)
        if not instruction.should_change:
            updated[svc_name] = current_dockerfile
            continue

        prompt = f"""You are a Dockerfile remediation agent.

Service: {svc_name}
Stack: {detected_stack}

USER FEEDBACK:
{feedback}

COORDINATOR INSTRUCTIONS:
{instruction.instructions}

CURRENT Dockerfile:
{current_dockerfile}

Rules:
- Apply coordinator instructions.
- Keep all currently-correct parts unchanged.
- Maintain production best practices (multi-stage, non-root) and do NOT include HEALTHCHECK instructions.
- Output ONLY raw Dockerfile content.
"""
        try:
            def _invoke_docker(raw_prompt: str):
                try:
                    return llm_docker.invoke(raw_prompt, config=config)
                except TypeError:
                    return llm_docker.invoke(raw_prompt)

            response, _, _ = invoke_with_retry(
                invoke_fn=_invoke_docker,
                prompt=prompt,
                fallback_prompt=FALLBACK_PROMPTS["docker"],
                config=RETRY_CONFIGS["docker"],
                node_name="feedback_dockerfile_improver",
            )
            improved = strip_markdown_wrapper(response.content, lang="dockerfile")
            mapped_service = service_by_dockerfile.get(svc_name, {})
            improved = _repair_dockerfile_output(
                improved,
                service={
                    "name": mapped_service.get("name", svc_name),
                    "build_context": mapped_service.get("build_context", "."),
                    "port": mapped_service.get("port", 8000),
                },
                key_files=scan.get("key_files", {}) if isinstance(scan, dict) else {},
                available_scripts=[],
            )
            updated[svc_name] = improved
        except Exception:
            updated[svc_name] = current_dockerfile

    state["dockerfiles"] = updated
    return state


def compose_improver_node(state: Dict[str, Any], config: RunnableConfig = None) -> Dict[str, Any]:
    current_compose = state.get("docker_compose") or ""
    detected_stack = state.get("detected_stack", "unknown")
    services = state.get("services", [])
    feedback = state.get("feedback", "")
    plan = state.get("change_plan", [])
    scan = state.get("repo_scan", {})

    instruction = _get_instruction(plan, artifact_type="compose")
    if not instruction.should_change:
        return state

    prompt = f"""You are a docker-compose remediation agent.

Stack: {detected_stack}
Services:
{json.dumps(services, indent=2)}

USER FEEDBACK:
{feedback}

COORDINATOR INSTRUCTIONS:
{instruction.instructions}

CURRENT docker-compose.yml:
{current_compose}

Rules:
- Apply coordinator instructions.
- Keep currently-correct services/ports/envs/volumes unchanged.
- Output ONLY raw YAML.
"""
    try:
        def _invoke_compose(raw_prompt: str):
            try:
                return llm_compose.invoke(raw_prompt, config=config)
            except TypeError:
                return llm_compose.invoke(raw_prompt)

        response, _, _ = invoke_with_retry(
            invoke_fn=_invoke_compose,
            prompt=prompt,
            fallback_prompt=FALLBACK_PROMPTS["compose"],
            config=RETRY_CONFIGS["compose"],
            node_name="feedback_compose_improver",
        )
        improved = strip_markdown_wrapper(response.content, lang="yaml")
        state["docker_compose"] = _repair_compose_output(improved, services, scan if isinstance(scan, dict) else {})
    except Exception:
        state["docker_compose"] = current_compose

    return state


def nginx_improver_node(state: Dict[str, Any], config: RunnableConfig = None) -> Dict[str, Any]:
    current_nginx = state.get("nginx_conf") or ""
    services = state.get("services", [])
    feedback = state.get("feedback", "")
    plan = state.get("change_plan", [])
    scan = state.get("repo_scan", {})
    _, _, include_ws = _infer_route_flags(scan if isinstance(scan, dict) else {}, services)

    instruction = _get_instruction(plan, artifact_type="nginx")
    if not instruction.should_change:
        return state

    prompt = f"""You are an nginx remediation agent.

Services:
{json.dumps(services, indent=2)}

USER FEEDBACK:
{feedback}

COORDINATOR INSTRUCTIONS:
{instruction.instructions}

CURRENT nginx.conf:
{current_nginx}

Rules:
- Apply coordinator instructions.
- Preserve currently-correct routes/security/proxy settings.
- Output ONLY raw nginx config.
"""
    try:
        def _invoke_nginx(raw_prompt: str):
            try:
                return llm_nginx.invoke(raw_prompt, config=config)
            except TypeError:
                return llm_nginx.invoke(raw_prompt)

        response, _, _ = invoke_with_retry(
            invoke_fn=_invoke_nginx,
            prompt=prompt,
            fallback_prompt=FALLBACK_PROMPTS["nginx"],
            config=RETRY_CONFIGS["nginx"],
            node_name="feedback_nginx_improver",
        )
        raw_nginx = strip_markdown_wrapper(response.content, lang="nginx")
        normalized = raw_nginx[5:] if raw_nginx.startswith("conf\n") else raw_nginx
        state["nginx_conf"] = _repair_nginx_output(normalized, services, include_ws=include_ws)
    except Exception:
        state["nginx_conf"] = current_nginx

    return state


def feedback_verifier_node(state: Dict[str, Any], config: RunnableConfig = None) -> Dict[str, Any]:
    dockerfiles = state.get("dockerfiles", {})
    docker_compose = state.get("docker_compose", "")
    nginx_conf = state.get("nginx_conf", "")
    feedback = state.get("feedback", "")
    services = state.get("services", [])
    detected_stack = state.get("detected_stack", "unknown")
    package_path = state.get("package_path", ".")
    build_verification = state.get("build_verification", {})

    hadolint_results: Dict[str, str] = {}
    for service_name, content in dockerfiles.items():
        hadolint_results[service_name] = run_hadolint(content)

    verifier_prompt = f"""
You are a senior DevOps reviewer. Review ALL updated deployment artifacts.

STACK: {detected_stack}
SERVICES: {json.dumps(services, indent=2)}

UPDATED DOCKERFILES:
{json.dumps(dockerfiles, indent=2)}

UPDATED COMPOSE:
{docker_compose}

UPDATED NGINX:
{nginx_conf}

HADOLINT RESULTS:
{json.dumps(hadolint_results, indent=2)}

USER FEEDBACK:
{feedback}

Return confidence (0.0-1.0) and risks list. Each risk must be one separate item.
"""

    try:
        def _invoke_verifier(raw_prompt: str):
            structured_llm = llm_verifier.with_structured_output(VerifierOutput)
            try:
                return structured_llm.invoke(raw_prompt, config=config)
            except TypeError:
                return structured_llm.invoke(raw_prompt)

        result, attempts_used, fallback_used = invoke_with_retry(
            invoke_fn=_invoke_verifier,
            prompt=verifier_prompt,
            fallback_prompt=FALLBACK_PROMPTS["verifier"],
            config=RETRY_CONFIGS["verifier"],
            node_name="feedback_verifier",
        )
        filtered_risks = _filter_risks(
            result.risks,
            services,
            dockerfiles,
            docker_compose,
            nginx_conf,
            package_path=package_path,
        )
        preflight_state = preflight_node(
            {
                "services": services,
                "dockerfiles": dockerfiles,
                "repo_scan": state.get("repo_scan", {}),
            }
        )
        preflight_issues = preflight_state.get("preflight_issues", []) if isinstance(preflight_state, dict) else []
        filtered_risks.extend([str(issue) for issue in preflight_issues])
        state["confidence"] = _compute_deterministic_confidence(
            services=services,
            dockerfiles=dockerfiles,
            docker_compose=docker_compose,
            nginx_conf=nginx_conf,
            risks=filtered_risks,
            build_verification=build_verification,
            preflight_issues=preflight_issues,
            package_path=package_path,
        )
        state["risks"] = filtered_risks
        llm_outputs = state.get("llm_outputs", {})
        if not isinstance(llm_outputs, dict):
            llm_outputs = {}
        llm_outputs["feedback_verifier"] = {
            "llm_confidence_raw": result.confidence,
            "llm_risks_raw": result.risks,
            "retry_attempts": attempts_used,
            "fallback_used": fallback_used,
            "preflight_issues": preflight_issues,
        }
        state["llm_outputs"] = llm_outputs
    except Exception as e:
        state["confidence"] = 0.5
        state["risks"] = [f"Verifier failed to run: {e}"]

    state["hadolint_results"] = hadolint_results
    return state


feedback_workflow = StateGraph(dict)
feedback_workflow.add_node("feedback_coordinator", feedback_coordinator_node)
feedback_workflow.add_node("dockerfile_improver", dockerfile_improver_node)
feedback_workflow.add_node("compose_improver", compose_improver_node)
feedback_workflow.add_node("nginx_improver", nginx_improver_node)
feedback_workflow.add_node("feedback_verifier", feedback_verifier_node)

feedback_workflow.set_entry_point("feedback_coordinator")
feedback_workflow.add_edge("feedback_coordinator", "dockerfile_improver")
feedback_workflow.add_edge("dockerfile_improver", "compose_improver")
feedback_workflow.add_edge("compose_improver", "nginx_improver")
feedback_workflow.add_edge("nginx_improver", "feedback_verifier")
feedback_workflow.add_edge("feedback_verifier", END)
feedback_graph = feedback_workflow.compile()


def build_feedback_initial_state(cached_result: Dict[str, Any], feedback: str) -> Dict[str, Any]:
    return {
        "feedback": feedback,
        "cached_result": cached_result,
        "commit_sha": cached_result.get("commit_sha", "unknown"),
        "detected_stack": cached_result.get("stack_summary", "unknown"),
        "stack_tokens": cached_result.get("stack_tokens", []),
        "services": cached_result.get("services", []),
        "dockerfiles": dict(cached_result.get("dockerfiles", {})),
        "docker_compose": cached_result.get("docker_compose") or "",
        "nginx_conf": cached_result.get("nginx_conf") or "",
        "has_existing_dockerfiles": cached_result.get("has_existing_dockerfiles", False),
        "has_existing_compose": cached_result.get("has_existing_compose", False),
        "prior_risks": list(cached_result.get("risks", [])),
        "prior_hadolint_results": dict(cached_result.get("hadolint_results", {})),
        "prior_feedbacks": list(cached_result.get("prior_feedbacks", [])),
        "feedback_round": cached_result.get("feedback_round", 0) + 1,
        "package_path": cached_result.get("_cache_package_path", "."),
        "repo_scan": cached_result.get("repo_scan", {}),
        "build_verification": cached_result.get("build_verification", {}),
        "llm_outputs": dict(cached_result.get("llm_outputs", {})),
    }

def format_feedback_result(result: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        "commit_sha": result.get("commit_sha", "unknown"),
        "stack_summary": result.get("detected_stack", "unknown"),
        "stack_tokens": result.get("stack_tokens", []),
        "services": result.get("services", []),
        "dockerfiles": result.get("dockerfiles", {}),
        "docker_compose": result.get("docker_compose"),
        "nginx_conf": result.get("nginx_conf"),
        "has_existing_dockerfiles": result.get("has_existing_dockerfiles", False),
        "has_existing_compose": result.get("has_existing_compose", False),
        "risks": result.get("risks", []),
        "confidence": result.get("confidence", 0.5),
        "hadolint_results": result.get("hadolint_results", {}),
        "llm_outputs": result.get("llm_outputs", {}),
        "feedback_round": result.get("feedback_round", 1),
    }
    prior_feedbacks = list(result.get("prior_feedbacks", []))
    current_feedback = result.get("feedback")
    if current_feedback and current_feedback not in prior_feedbacks:
        prior_feedbacks.append(current_feedback)
    out["prior_feedbacks"] = prior_feedbacks
    return out


def run_feedback_improvement(
    cached_result: Dict[str, Any],
    feedback: str,
    context: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Run multi-agent feedback remediation and return AnalyzeResponse-shaped data."""
    initial_state = build_feedback_initial_state(cached_result, feedback)
    if isinstance(context, dict):
        initial_state.update(context)
    result = feedback_graph.invoke(initial_state)
    return format_feedback_result(result)
