from typing import Dict, Any
import os
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


def _service_requires_compose_networking(service: Dict[str, Any]) -> bool:
    """Return True when a single service still benefits from compose networking.

    This applies to monorepo-root builds that target a nested dockerfile path
    (e.g. build_context='.' + dockerfile_path='apps/dashboard/Dockerfile').
    """
    if not isinstance(service, dict):
        return False
    build_ctx = _normalize_package_path(str(service.get("build_context", ".") or "."))
    dockerfile_path = str(service.get("dockerfile_path", "") or "").replace("\\", "/").strip()
    return build_ctx == "." and "/" in dockerfile_path


def _hydrate_root_workspace_signals(
    scan: Dict[str, Any],
    *,
    repo_url: str,
    github_token: str | None,
    max_files: int,
    package_path: str,
) -> Dict[str, Any]:
    """For scoped package analysis, merge root workspace manifests into scan context."""
    if _normalize_package_path(package_path) == ".":
        return scan
    if not isinstance(scan, dict):
        return scan

    key_files = scan.get("key_files", {})
    if not isinstance(key_files, dict):
        key_files = {}

    # If scoped scan already has explicit workspace markers, avoid extra GitHub call.
    # Do not treat a plain `package.json` as sufficient here because in scoped mode
    # it can belong to the package itself (not the repo root workspace).
    workspace_markers = (
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "turbo.json",
        "turbo.yaml",
        "nx.json",
        "lerna.json",
    )
    if any(name in key_files for name in workspace_markers):
        return scan

    try:
        root_scan = fetch_repo_structure.invoke(
            {
                "repo_url": repo_url,
                "github_token": github_token,
                "max_files": max(max_files, 200),
                "package_path": ".",
            }
        )
    except Exception as e:
        print(f"Workspace signal hydration failed: {e}")
        return scan

    if not isinstance(root_scan, dict) or root_scan.get("error"):
        return scan

    root_key_files = root_scan.get("key_files", {})
    if not isinstance(root_key_files, dict):
        root_key_files = {}

    merged = dict(scan)
    merged_key_files = dict(key_files)
    for filename in ("package.json", *workspace_markers):
        value = root_key_files.get(filename)
        if isinstance(value, str) and value.strip():
            merged_key_files.setdefault(filename, value)
    merged["key_files"] = merged_key_files

    # Preserve top-level directory hints used by downstream monorepo heuristics.
    scoped_dirs = scan.get("dirs", []) if isinstance(scan.get("dirs", []), list) else []
    root_dirs = root_scan.get("dirs", []) if isinstance(root_scan.get("dirs", []), list) else []
    if root_dirs:
        merged_dirs = set(str(d) for d in scoped_dirs)
        for d in root_dirs:
            d_str = str(d).strip()
            if "/" not in d_str and d_str:
                merged_dirs.add(d_str)
        merged["dirs"] = sorted(merged_dirs)

    # Pass explicit root-workspace hints for planner logic in scoped mode.
    merged["_root_workspace_detected"] = any(name in root_key_files for name in workspace_markers)
    sub_packages: list[str] = []
    for file_path in root_key_files.keys():
        norm = _normalize_package_path(str(file_path))
        if norm.endswith("/package.json"):
            parent = norm[: -len("/package.json")]
            if parent and "/" in parent:
                sub_packages.append(parent)
    merged["_root_workspace_sub_packages"] = sorted(set(sub_packages))

    return merged


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

    keep_compose = len(filtered_services) == 1 and _service_requires_compose_networking(filtered_services[0])
    projected = dict(cached)
    projected["services"] = filtered_services
    projected["dockerfiles"] = filtered_dockerfiles
    projected["hadolint_results"] = filtered_hadolint
    if not keep_compose:
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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return max(minimum, value)
    except ValueError:
        return default


def _maybe_build_scope_guard_error(scan: Dict[str, Any], package_path: str, service_name: str | None) -> Dict[str, Any] | None:
    if not _env_bool("SD_SCOPE_GUARD_ENABLED", True):
        return None
    if _normalize_package_path(package_path) != ".":
        return None
    if _normalize_service_name(service_name):
        return None

    tree_threshold = _env_int("SD_SCOPE_GUARD_TREE_THRESHOLD", 3000)
    package_threshold = _env_int("SD_SCOPE_GUARD_PACKAGE_THRESHOLD", 20)
    tree_count = int(scan.get("tree_entry_count") or 0)
    candidate_paths = scan.get("candidate_package_paths") or []
    if not isinstance(candidate_paths, list):
        candidate_paths = []
    candidate_count = len(candidate_paths)

    if tree_count <= tree_threshold and candidate_count <= package_threshold:
        return None

    service_hints = scan.get("candidate_service_hints") or []
    if not isinstance(service_hints, list):
        service_hints = []

    return {
        "code": "scope_required",
        "reason": (
            "Repository scope is too broad for root analysis. "
            "Specify package_path or service_name to narrow analysis."
        ),
        "tree_entry_count": tree_count,
        "candidate_package_count": candidate_count,
        "suggested_package_paths": candidate_paths[:10],
        "suggested_service_names": service_hints[:10],
    }


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

    keep_compose = _service_requires_compose_networking(selected_service) or bool(cached.get("docker_compose"))
    projected = dict(cached)
    projected["services"] = [selected_service]
    projected["dockerfiles"] = filtered_dockerfiles
    projected["hadolint_results"] = filtered_hadolint
    if not keep_compose:
        projected["docker_compose"] = None
        projected["nginx_conf"] = None
    return projected

def scanner_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Calls GitHub tool directly to populate repo_scan. Also checks cache."""
    repo_url = state["repo_url"]
    github_token = state.get("github_token")
    max_files = int(state.get("max_files", 50) or 50)
    package_path = state.get("package_path", ".")
    scan = fetch_repo_structure.invoke(
        {
            "repo_url": repo_url,
            "github_token": github_token,
            "max_files": max_files,
            "package_path": package_path,
        }
    )
    
    if "error" in scan:
        state["error"] = scan["error"]
        return state
        
    scan = _hydrate_root_workspace_signals(
        scan,
        repo_url=repo_url,
        github_token=github_token,
        max_files=max_files,
        package_path=package_path,
    )

    commit_sha = scan.get("commit_sha", "unknown")
    state["commit_sha"] = commit_sha
    requested_package_path = _normalize_package_path(state.get("package_path", "."))
    requested_service_name = _normalize_service_name(state.get("service_name"))

    scope_guard_error = _maybe_build_scope_guard_error(
        scan=scan,
        package_path=requested_package_path,
        service_name=requested_service_name,
    )
    if scope_guard_error:
        state["error"] = scope_guard_error
        state["repo_scan"] = scan
        return state
    
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
