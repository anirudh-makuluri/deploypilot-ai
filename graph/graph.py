from langgraph.graph import StateGraph, END
from langchain_core.runnables.config import RunnableConfig
from typing import Dict, Any, TypedDict

from .nodes import (
    scanner_node,
    planner_node,
    dockerfile_generator_node,
    commands_generator_node,
    compose_generator_node,
    nginx_generator_node,
    verifier_node,
    build_verify_node,
    preflight_node,
)


class StateDict(TypedDict, total=False):
    """State schema for the artifact generation workflow."""
    error: str | None
    cached_response: Dict[str, Any] | None
    services: list | None
    package_path: str
    repo_url: str
    github_token: str | None
    scan_results: Dict[str, Any]
    plan: Dict[str, Any]
    commands: list | None
    docker_generated: str | None
    compose_generated: str | None
    nginx_generated: str | None
    verification_results: Dict[str, Any] | None
    preflight_checks: Dict[str, Any] | None
    final_output: Dict[str, Any] | None


workflow = StateGraph(StateDict)

# Wrap nodes to handle RunnableConfig parameter properly
def _wrap_node_with_config(node_func):
    """Wrap node function to handle optional RunnableConfig parameter."""
    def wrapper(state: StateDict, config: RunnableConfig | None = None) -> StateDict:  # type: ignore
        if config is not None:
            return node_func(state, config=config)  # type: ignore
        return node_func(state)  # type: ignore
    return wrapper

def _wrap_node_simple(node_func):
    """Wrap simple node function without config parameter."""
    def wrapper(state: StateDict) -> StateDict:  # type: ignore
        return node_func(state)  # type: ignore
    return wrapper

workflow.add_node("scanner", _wrap_node_simple(scanner_node))
workflow.add_node("planner", _wrap_node_with_config(planner_node))
workflow.add_node("commands_gen", _wrap_node_simple(commands_generator_node))
workflow.add_node("docker_gen", _wrap_node_with_config(dockerfile_generator_node))
workflow.add_node("compose_gen", _wrap_node_with_config(compose_generator_node))
workflow.add_node("nginx_gen", _wrap_node_with_config(nginx_generator_node))
workflow.add_node("build_verify", _wrap_node_simple(build_verify_node))
workflow.add_node("preflight", _wrap_node_simple(preflight_node))
workflow.add_node("verifier", _wrap_node_with_config(verifier_node))


# ─── Conditional Edges ──────────────────────────────────────────────────────────

def check_scanner_error(state: Dict[str, Any]) -> str:
    """Route to END if scanner found an error or if cached_response is present."""
    if state.get("error") or state.get("cached_response"):
        return "error_or_cached"
    return "continue"

def check_planner_error(state: Dict[str, Any]) -> str:
    """Route to END if planner found the repo is not deployable."""
    return "error" if state.get("error") else "continue"


def check_compose_required(state: Dict[str, Any]) -> str:
    """Generate compose only when there are multiple app services."""
    services = state.get("services")
    if not isinstance(services, list) or len(services) <= 1:
        return "skip"
        
    package_path = state.get("package_path", ".")
    from graph.nodes.planner import _normalize_ctx
    package_norm = _normalize_ctx(package_path)
    
    # If all services share the exact same expected container logic path, and it's an explicit 
    # sub-package deploy, we don't need compose - it's a single container.
    if package_norm != ".":
        all_same_context = all(
            _normalize_ctx(svc.get("build_context", ".")) == package_norm or 
            (_normalize_ctx(svc.get("dockerfile_path", "")) and _normalize_ctx(svc.get("dockerfile_path", "")).startswith(package_norm))
            for svc in services
        )
        if all_same_context:
            return "skip"
            
    return "compose"


def is_build_verify_enabled() -> bool:
    import os
    raw = os.getenv("SD_RAILPACK_VERIFY_ENABLED", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def check_build_verify_required(_state: Dict[str, Any]) -> str:
    return "verify" if is_build_verify_enabled() else "skip"


# Entry point
workflow.set_entry_point("scanner")

# Scanner -> Planner (or END on error/cache)
workflow.add_conditional_edges(
    "scanner",
    check_scanner_error,
    {
        "error_or_cached": END,
        "continue": "planner",
    },
)

# Planner -> Commands gen (or END on error)
workflow.add_conditional_edges(
    "planner",
    check_planner_error,
    {
        "error": END,
        "continue": "commands_gen",
    },
)

# Flow: commands_gen -> docker_gen -> compose_gen (if needed) -> nginx_gen -> (optional build_verify) -> preflight -> verifier -> END
workflow.add_edge("commands_gen", "docker_gen")
workflow.add_conditional_edges(
    "docker_gen",
    check_compose_required,
    {
        "compose": "compose_gen",
        "skip": "nginx_gen",
    },
)
workflow.add_edge("compose_gen", "nginx_gen")
workflow.add_conditional_edges(
    "nginx_gen",
    check_build_verify_required,
    {
        "verify": "build_verify",
        "skip": "preflight",
    },
)
workflow.add_edge("build_verify", "preflight")
workflow.add_edge("preflight", "verifier")
workflow.add_edge("verifier", END)

graph = workflow.compile()
