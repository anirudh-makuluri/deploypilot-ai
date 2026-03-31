from typing import Dict, Any
from tools.github_tools import fetch_repo_structure
from db import supabase


def _normalize_package_path(path: str) -> str:
    """Normalize package paths to a stable representation."""
    normalized = (path or ".").replace("\\", "/").strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized or "."


def _get_dockerfile_path(build_context: str) -> str:
    """Generate the dockerfile path from build context.
    
    Examples:
    - "." -> "Dockerfile"
    - "" -> "Dockerfile"
    - "client" -> "client/Dockerfile"
    - "./client" -> "client/Dockerfile"
    """
    normalized = _normalize_package_path(build_context)
    if normalized in (".", ""):
        return "Dockerfile"
    return f"{normalized}/Dockerfile"


def _path_is_within(service_path: str, package_path: str) -> bool:
    """Return True when service_path is package_path or a descendant of it."""
    service_norm = _normalize_package_path(service_path)
    package_norm = _normalize_package_path(package_path)

    if package_norm == ".":
        return True
    if service_norm == package_norm:
        return True
    return service_norm.startswith(package_norm + "/")


def _filter_cached_response_for_package(cached: Dict[str, Any], package_path: str) -> Dict[str, Any] | None:
    """Project a full cached response down to the requested package path."""
    package_norm = _normalize_package_path(package_path)
    if package_norm == ".":
        return cached

    services = cached.get("services", [])
    if not isinstance(services, list):
        return None

    filtered_services = []
    for svc in services:
        build_ctx = svc.get("build_context", ".") if isinstance(svc, dict) else "."
        if _path_is_within(build_ctx, package_norm):
            filtered_services.append(svc)

    if not filtered_services:
        return None

    # Build a set of dockerfile paths for the filtered services
    dockerfile_paths = {
        _get_dockerfile_path(svc.get("build_context", "."))
        for svc in filtered_services
        if isinstance(svc, dict)
    }

    dockerfiles = cached.get("dockerfiles", {})
    filtered_dockerfiles = {
        path: content
        for path, content in dockerfiles.items()
        if path in dockerfile_paths
    } if isinstance(dockerfiles, dict) else {}

    hadolint_results = cached.get("hadolint_results", {})
    filtered_hadolint = {
        path: result
        for path, result in hadolint_results.items()
        if path in dockerfile_paths
    } if isinstance(hadolint_results, dict) else {}

    projected = dict(cached)
    projected["services"] = filtered_services
    projected["dockerfiles"] = filtered_dockerfiles
    projected["hadolint_results"] = filtered_hadolint
    projected["docker_compose"] = None
    projected["nginx_conf"] = None
    projected["_cache_package_path"] = package_norm
    return projected


def _pick_best_cached_response(cached_rows: list, requested_package_path: str) -> Dict[str, Any] | None:
    """Choose the most useful cached response for the request."""
    requested_norm = _normalize_package_path(requested_package_path)
    candidates = [row.get("result", {}) for row in cached_rows if isinstance(row, dict)]

    if not candidates:
        return None

    if requested_norm == ".":
        # Prefer full-repo cache for full-repo requests.
        for candidate in candidates:
            if _normalize_package_path(candidate.get("_cache_package_path", ".")) == ".":
                return candidate
        return candidates[0]

    # For package requests, only return an exact package cache match.
    # Reusing full-repo cache rows here can leak repo-wide analysis details
    # (for example stack summary/tokens/risks) into package-scoped requests.
    for candidate in candidates:
        if _normalize_package_path(candidate.get("_cache_package_path", "")) == requested_norm:
            return candidate

    return None


def _normalize_service_name(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def _filter_cached_response_for_service(cached: Dict[str, Any], service_name: str | None) -> Dict[str, Any] | None:
    """Filter a cached analysis payload down to a single requested service.

    This mirrors the planner's service selector behavior but works on cached dict payloads.
    Returns None when the selector matches zero or multiple services.
    """
    selector = (service_name or "").strip().lower()
    if not selector:
        return cached

    services = cached.get("services", [])
    if not isinstance(services, list):
        return None

    def _norm_ctx(value: str) -> str:
        return _normalize_package_path(value).lower()

    def _norm_file(value: str) -> str:
        return (value or "").replace("\\", "/").strip().lower()

    matches = [svc for svc in services if isinstance(svc, dict) and (svc.get("name") or "").strip().lower() == selector]
    if not matches:
        selector_ctx = _norm_ctx(service_name or "")
        matches = [svc for svc in services if isinstance(svc, dict) and _norm_ctx(str(svc.get("build_context", "."))) == selector_ctx]
    if not matches:
        selector_file = _norm_file(service_name or "")
        matches = [svc for svc in services if isinstance(svc, dict) and _norm_file(str(svc.get("dockerfile_path", ""))) == selector_file]
    if not matches:
        matches = [
            svc
            for svc in services
            if isinstance(svc, dict)
            and (
                selector in (svc.get("name") or "").lower()
                or selector in _norm_ctx(str(svc.get("build_context", ".")))
                or selector in _norm_file(str(svc.get("dockerfile_path", "")))
            )
        ]

    if len(matches) != 1:
        return None

    selected_service = matches[0]

    # Filter dockerfiles/hadolint to the selected service's dockerfile path (if known).
    dockerfile_paths: set[str] = set()
    svc_df = selected_service.get("dockerfile_path") if isinstance(selected_service, dict) else ""
    if isinstance(svc_df, str) and svc_df.strip():
        dockerfile_paths.add(svc_df.replace("\\", "/").strip())
    else:
        dockerfile_paths.add(_get_dockerfile_path(str(selected_service.get("build_context", "."))))

    dockerfiles = cached.get("dockerfiles", {})
    filtered_dockerfiles = {
        path: content
        for path, content in dockerfiles.items()
        if isinstance(dockerfiles, dict) and path in dockerfile_paths
    } if isinstance(dockerfiles, dict) else {}

    hadolint_results = cached.get("hadolint_results", {})
    filtered_hadolint = {
        path: result
        for path, result in hadolint_results.items()
        if isinstance(hadolint_results, dict) and path in dockerfile_paths
    } if isinstance(hadolint_results, dict) else {}

    projected = dict(cached)
    projected["services"] = [selected_service]
    projected["dockerfiles"] = filtered_dockerfiles
    projected["hadolint_results"] = filtered_hadolint
    projected["docker_compose"] = None
    projected["nginx_conf"] = None
    return projected

def scanner_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Calls GitHub tool directly to populate repo_scan. Also checks cache."""
    scan = fetch_repo_structure.invoke({
        "repo_url": state["repo_url"],
        "github_token": state.get("github_token"),
        "max_files": state.get("max_files", 50),
        "package_path": state.get("package_path", ".")
    })
    
    if "error" in scan:
        state["error"] = scan["error"]
        return state
        
    commit_sha = scan.get("commit_sha", "unknown")
    state["commit_sha"] = commit_sha
    requested_package_path = _normalize_package_path(state.get("package_path", "."))
    requested_service_name = _normalize_service_name(state.get("service_name"))
    
    if supabase and commit_sha != "unknown":
        for attempt in range(3):
            try:
                query = (
                    supabase.table("analysis_cache")
                    .select("result")
                    .eq("repo_url", state["repo_url"])
                    .eq("commit_sha", commit_sha)
                    .eq("package_path", requested_package_path)
                )
                # Cache is keyed by service_name too; null means "full analysis".
                if requested_service_name is None:
                    query = query.is_("service_name", None)
                else:
                    query = query.eq("service_name", requested_service_name)
                response = query.execute()
                if response.data and len(response.data) > 0:
                    cached = _pick_best_cached_response(response.data, requested_package_path)
                    if cached:
                        # Backward-compat: if an older cache row is returned for a service-scoped
                        # request, project it down to the selected service.
                        if requested_service_name:
                            projected = _filter_cached_response_for_service(cached, requested_service_name)
                            if not projected:
                                break
                            cached = projected
                        state["cached_response"] = cached
                        state["repo_scan"] = scan
                        return state
                break  # Query succeeded but returned no data, exit retry loop
            except Exception as e:
                print(f"Supabase cache read error (attempt {attempt + 1}/3): {e}")
                if attempt < 2:
                    import time
                    time.sleep(1)
    
    state["repo_scan"] = scan
    return state

