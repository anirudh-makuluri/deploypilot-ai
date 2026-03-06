from typing import Dict, Any
from tools.github_tools import fetch_repo_structure


def scanner_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Calls GitHub tool directly to populate repo_scan."""
    scan = fetch_repo_structure.invoke({
        "repo_url": state["repo_url"],
        "github_token": state["github_token"],
        "max_files": state.get("max_files", 50),
        "package_path": state.get("package_path", ".")
    })
    
    if "error" in scan:
        state["error"] = scan["error"]
        return state
    
    state["repo_scan"] = scan
    return state
