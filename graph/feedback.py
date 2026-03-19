from typing import Any, Dict, List, Literal
import json

from langgraph.graph import StateGraph, END
from pydantic import BaseModel, Field

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

    for svc_name in dockerfiles.keys():
        plan.append(
            ChangeInstruction(
                artifact_type="dockerfile",
                service_name=svc_name,
                should_change=True,
                instructions=f"Coordinator fallback ({reason}): Apply feedback safely. Feedback: {feedback}",
            )
        )

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


def feedback_coordinator_node(state: Dict[str, Any]) -> Dict[str, Any]:
    services = state.get("services", [])
    detected_stack = state.get("detected_stack", "unknown")
    dockerfiles = state.get("dockerfiles", {})
    docker_compose = state.get("docker_compose") or ""
    nginx_conf = state.get("nginx_conf") or ""
    prior_risks = state.get("prior_risks", [])
    prior_hadolint = state.get("prior_hadolint_results", {})
    feedback = state.get("feedback", "")

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

CURRENT DOCKERFILES:
{json.dumps(dockerfiles, indent=2)}

CURRENT DOCKER-COMPOSE:
{docker_compose}

CURRENT NGINX:
{nginx_conf}

Return a structured plan with one dockerfile instruction per service, plus one each for compose and nginx.
Set should_change=false when a file does not need modification.
Keep instructions concise, actionable, and file-specific.
"""

    try:
        def _invoke(raw_prompt: str):
            structured = llm_coordinator.with_structured_output(CoordinatorOutput)
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


def dockerfile_improver_node(state: Dict[str, Any]) -> Dict[str, Any]:
    dockerfiles = state.get("dockerfiles", {})
    detected_stack = state.get("detected_stack", "unknown")
    feedback = state.get("feedback", "")
    plan = state.get("change_plan", [])
    updated: Dict[str, str] = {}

    for svc_name, current_dockerfile in dockerfiles.items():
        instruction = _get_instruction(plan, artifact_type="dockerfile", service_name=svc_name)
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
            response, _, _ = invoke_with_retry(
                invoke_fn=lambda raw_prompt: llm_docker.invoke(raw_prompt),
                prompt=prompt,
                fallback_prompt=FALLBACK_PROMPTS["docker"],
                config=RETRY_CONFIGS["docker"],
                node_name="feedback_dockerfile_improver",
            )
            updated[svc_name] = strip_markdown_wrapper(response.content, lang="dockerfile")
        except Exception:
            updated[svc_name] = current_dockerfile

    state["dockerfiles"] = updated
    return state


def compose_improver_node(state: Dict[str, Any]) -> Dict[str, Any]:
    current_compose = state.get("docker_compose") or ""
    detected_stack = state.get("detected_stack", "unknown")
    services = state.get("services", [])
    feedback = state.get("feedback", "")
    plan = state.get("change_plan", [])

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
        response, _, _ = invoke_with_retry(
            invoke_fn=lambda raw_prompt: llm_compose.invoke(raw_prompt),
            prompt=prompt,
            fallback_prompt=FALLBACK_PROMPTS["compose"],
            config=RETRY_CONFIGS["compose"],
            node_name="feedback_compose_improver",
        )
        state["docker_compose"] = strip_markdown_wrapper(response.content, lang="yaml")
    except Exception:
        state["docker_compose"] = current_compose

    return state


def nginx_improver_node(state: Dict[str, Any]) -> Dict[str, Any]:
    current_nginx = state.get("nginx_conf") or ""
    services = state.get("services", [])
    feedback = state.get("feedback", "")
    plan = state.get("change_plan", [])

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
        response, _, _ = invoke_with_retry(
            invoke_fn=lambda raw_prompt: llm_nginx.invoke(raw_prompt),
            prompt=prompt,
            fallback_prompt=FALLBACK_PROMPTS["nginx"],
            config=RETRY_CONFIGS["nginx"],
            node_name="feedback_nginx_improver",
        )
        raw_nginx = strip_markdown_wrapper(response.content, lang="nginx")
        state["nginx_conf"] = raw_nginx[5:] if raw_nginx.startswith("conf\n") else raw_nginx
    except Exception:
        state["nginx_conf"] = current_nginx

    return state


def feedback_verifier_node(state: Dict[str, Any]) -> Dict[str, Any]:
    dockerfiles = state.get("dockerfiles", {})
    docker_compose = state.get("docker_compose", "")
    nginx_conf = state.get("nginx_conf", "")
    feedback = state.get("feedback", "")
    services = state.get("services", [])
    detected_stack = state.get("detected_stack", "unknown")

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
            return structured_llm.invoke(raw_prompt)

        result, _, _ = invoke_with_retry(
            invoke_fn=_invoke_verifier,
            prompt=verifier_prompt,
            fallback_prompt=FALLBACK_PROMPTS["verifier"],
            config=RETRY_CONFIGS["verifier"],
            node_name="feedback_verifier",
        )
        state["confidence"] = result.confidence
        state["risks"] = result.risks
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
    }

def format_feedback_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
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
    }


def run_feedback_improvement(cached_result: Dict[str, Any], feedback: str) -> Dict[str, Any]:
    """Run multi-agent feedback remediation and return AnalyzeResponse-shaped data."""
    initial_state = build_feedback_initial_state(cached_result, feedback)
    result = feedback_graph.invoke(initial_state)
    return format_feedback_result(result)
