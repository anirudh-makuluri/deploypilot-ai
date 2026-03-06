from langgraph.graph import StateGraph, END
from typing import Dict, Any

from .nodes import (
    scanner_node,
    planner_node,
    dockerfile_generator_node,
    compose_generator_node,
    nginx_generator_node
)

# State is a plain dict
workflow = StateGraph(dict)

workflow.add_node("scanner", scanner_node)
workflow.add_node("planner", planner_node)
workflow.add_node("compose_gen", compose_generator_node)
workflow.add_node("docker_gen", dockerfile_generator_node)
workflow.add_node("nginx_gen", nginx_generator_node)

# Edges
workflow.add_edge("scanner", "planner")

def needs_compose(state: Dict[str, Any]) -> str:
    return "compose" if state.get("services_needed") else "no_compose"

workflow.add_conditional_edges(
    "planner",
    needs_compose,
    {
        "compose": "compose_gen",
        "no_compose": "docker_gen",
    },
)

workflow.add_edge("compose_gen", "docker_gen")
workflow.add_edge("docker_gen", "nginx_gen")
workflow.add_edge("nginx_gen", END)

# Entry point
workflow.set_entry_point("scanner")

graph = workflow.compile()
