from langgraph.graph import StateGraph, END
from typing import Dict, Any

from .nodes import (
    scanner_node,
    planner_node,
    dockerfile_generator_node,
    commands_generator_node,
    compose_generator_node,
    nginx_generator_node,
    verifier_node
)

# State is a plain dict
workflow = StateGraph(dict)

workflow.add_node("scanner", scanner_node)
workflow.add_node("planner", planner_node)
workflow.add_node("commands_gen", commands_generator_node)
workflow.add_node("docker_gen", dockerfile_generator_node)
workflow.add_node("compose_gen", compose_generator_node)
workflow.add_node("nginx_gen", nginx_generator_node)
workflow.add_node("verifier", verifier_node)


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

# Flow: commands_gen -> docker_gen -> compose_gen (if needed) -> nginx_gen -> verifier -> END
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
workflow.add_edge("nginx_gen", "verifier")
workflow.add_edge("verifier", END)

graph = workflow.compile()
