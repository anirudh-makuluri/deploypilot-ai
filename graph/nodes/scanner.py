from typing import Dict, Any
from tools.github_tools import fetch_repo_structure
from db import supabase

def scanner_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Calls GitHub tool directly to populate repo_scan. Also checks cache."""
    scan = fetch_repo_structure.invoke({
        "repo_url": state["repo_url"],
        "github_token": state["github_token"],
        "max_files": state.get("max_files", 50),
        "package_path": state.get("package_path", ".")
    })
    
    if "error" in scan:
        state["error"] = scan["error"]
        return state
        
    commit_sha = scan.get("commit_sha", "unknown")
    state["commit_sha"] = commit_sha
    
    if supabase and commit_sha != "unknown":
        try:
            response = supabase.table("analysis_cache").select("result").eq("repo_url", state["repo_url"]).eq("commit_sha", commit_sha).execute()
            if response.data and len(response.data) > 0:
                print(f"Cache hit for {state['repo_url']} at {commit_sha}")
                state["cached_response"] = response.data[0]["result"]
                return state
        except Exception as e:
            print(f"Supabase cache read error: {e}")
    
    state["repo_scan"] = scan
    return state

