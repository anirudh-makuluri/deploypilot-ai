from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import time
from typing import Optional

import requests
from langchain_core.tools import tool
from pydantic import BaseModel, Field


ROOT_MARKDOWN_FILES = {
    "readme.md",
    "deployment.md",
    "deploy.md",
    "architecture.md",
    "overview.md",
    "setup.md",
}

PACKAGE_MARKDOWN_FILES = ROOT_MARKDOWN_FILES | {
    "notes.md",
    "runbook.md",
}


class RepoScanInput(BaseModel):
    repo_url: str = Field(..., description="Full GitHub repo URL")
    github_token: Optional[str] = Field(None, description="Optional GitHub token (required for private repos)")
    max_files: Optional[int] = Field(20, description="Max files to analyze")
    package_path: str = Field(".", description="Sub-package path to analyze, '.' for entire repo")


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


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return max(minimum, value)
    except ValueError:
        return default


def _settings() -> dict:
    return {
        "fetch_markdown": _env_bool("SD_FETCH_MARKDOWN", False),
        "api_connect_timeout": _env_float("SD_GITHUB_CONNECT_TIMEOUT_SECONDS", 5.0, 0.1),
        "api_read_timeout": _env_float("SD_GITHUB_READ_TIMEOUT_SECONDS", 20.0, 0.1),
        "api_attempts": _env_int("SD_GITHUB_HTTP_ATTEMPTS", 3, 1),
        "api_backoff_seconds": _env_float("SD_GITHUB_RETRY_BACKOFF_SECONDS", 0.5, 0.0),
        "raw_connect_timeout": _env_float("SD_GITHUB_RAW_CONNECT_TIMEOUT_SECONDS", 3.0, 0.1),
        "raw_read_timeout": _env_float("SD_GITHUB_RAW_READ_TIMEOUT_SECONDS", 8.0, 0.1),
        "raw_attempts": _env_int("SD_GITHUB_RAW_HTTP_ATTEMPTS", 2, 1),
        "raw_backoff_seconds": _env_float("SD_GITHUB_RAW_RETRY_BACKOFF_SECONDS", 0.2, 0.0),
        "raw_max_workers": _env_int("SD_GITHUB_RAW_MAX_WORKERS", 6, 1),
        "include_root_config_for_package": _env_bool("SD_INCLUDE_ROOT_CONFIG_FOR_PACKAGE", True),
    }


def _normalize_path(value: str) -> str:
    normalized = (value or ".").replace("\\", "/").strip().strip("/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized or "."


def _is_relevant_markdown_file(path: str, package_path: str) -> bool:
    normalized_path = _normalize_path(path)
    lower_path = normalized_path.lower()
    if not lower_path.endswith(".md"):
        return False

    file_name = lower_path.rsplit("/", 1)[-1]
    normalized_package_path = _normalize_path(package_path)

    if normalized_package_path == ".":
        return "/" not in lower_path and file_name in ROOT_MARKDOWN_FILES

    parent_path = lower_path.rsplit("/", 1)[0] if "/" in lower_path else "."
    return parent_path == normalized_package_path.lower() and file_name in PACKAGE_MARKDOWN_FILES


def _is_deploy_relevant_blob(path: str, package_path: str, fetch_markdown: bool) -> bool:
    normalized = _normalize_path(path)
    lower_name = normalized.rsplit("/", 1)[-1].lower()

    exact_names = {
        "package.json",
        "requirements.txt",
        "pyproject.toml",
        "pipfile",
        "pipfile.lock",
        "poetry.lock",
        "uv.lock",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "yarn.lock",
        "package-lock.json",
        "bun.lock",
        "bun.lockb",
        "go.mod",
        "go.sum",
        "cargo.toml",
        "cargo.lock",
        "gemfile",
        "gemfile.lock",
        "composer.json",
        "composer.lock",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        "nginx.conf",
        "turbo.json",
        "turbo.yaml",
        "next.config.js",
        "next.config.mjs",
        "next.config.ts",
        "vite.config.js",
        "vite.config.ts",
        "nuxt.config.js",
        "nuxt.config.ts",
        "caddyfile",
    }

    if lower_name in exact_names:
        return True
    if lower_name == "dockerfile" or lower_name.startswith("dockerfile.") or lower_name.endswith(".dockerfile"):
        return True
    if fetch_markdown and _is_relevant_markdown_file(normalized, package_path):
        return True
    return False


def _http_get(url: str, headers: dict, policy: str, settings: dict):
    if policy == "raw":
        connect_timeout = settings["raw_connect_timeout"]
        read_timeout = settings["raw_read_timeout"]
        attempts = settings["raw_attempts"]
        backoff = settings["raw_backoff_seconds"]
    else:
        connect_timeout = settings["api_connect_timeout"]
        read_timeout = settings["api_read_timeout"]
        attempts = settings["api_attempts"]
        backoff = settings["api_backoff_seconds"]

    timeout = (connect_timeout, read_timeout)
    for attempt in range(1, attempts + 1):
        try:
            # Backward compatibility for tests monkeypatching requests.get without timeout kwarg.
            try:
                return requests.get(url, headers=headers, timeout=timeout)
            except TypeError:
                return requests.get(url, headers=headers)
        except requests.RequestException:
            if attempt >= attempts:
                return None
            time.sleep(backoff * attempt)
    return None


def _navigate_to_subtree(repo: str, branch: str, package_path: str, headers: dict, settings: dict):
    """Walk path components to the target directory and fetch its subtree recursively."""
    components = [c for c in package_path.split("/") if c]
    current_ref = branch

    for component in components:
        level_resp = _http_get(
            f"https://api.github.com/repos/{repo}/git/trees/{current_ref}",
            headers=headers,
            policy="api",
            settings=settings,
        )
        if not level_resp:
            return [], "Unable to fetch repository tree due to network timeout/retry exhaustion"
        if level_resp.status_code in (401, 403):
            return [], "Unable to fetch repository tree due to authentication/rate-limit restrictions"
        if level_resp.status_code != 200:
            return [], f"Package path '{package_path}' not found in repository"
        entries = level_resp.json().get("tree", [])
        match = next(
            (e for e in entries if e.get("path") == component and e.get("type") == "tree"),
            None,
        )
        if not match:
            return [], f"Package path '{package_path}' not found in repository"
        current_ref = match["sha"]

    subtree_resp = _http_get(
        f"https://api.github.com/repos/{repo}/git/trees/{current_ref}?recursive=1",
        headers=headers,
        policy="api",
        settings=settings,
    )
    if not subtree_resp:
        return [], "Unable to fetch repository tree due to network timeout/retry exhaustion"
    if subtree_resp.status_code in (401, 403):
        return [], "Unable to fetch repository tree due to authentication/rate-limit restrictions"
    if subtree_resp.status_code != 200:
        return [], f"Package path '{package_path}' not found in repository"

    prefix = package_path + "/"
    items = [
        {**item, "path": prefix + item["path"]}
        for item in subtree_resp.json().get("tree", [])
    ]
    return items, None


def _infer_candidate_package_paths(all_items: list) -> list[str]:
    manifest_names = {
        "package.json",
        "requirements.txt",
        "pyproject.toml",
        "go.mod",
        "cargo.toml",
        "gemfile",
        "composer.json",
    }
    candidates = set()
    for item in all_items:
        if item.get("type") != "blob":
            continue
        path = str(item.get("path", ""))
        if not path:
            continue
        lower_name = path.rsplit("/", 1)[-1].lower()
        if lower_name not in manifest_names:
            continue
        parent = path.rsplit("/", 1)[0] if "/" in path else "."
        candidates.add(_normalize_path(parent))
    return sorted(candidates)


def _infer_candidate_service_hints(all_items: list, package_paths: list[str]) -> list[str]:
    hints = set()
    for path in package_paths:
        name = path.rsplit("/", 1)[-1] if path != "." else "root"
        if name:
            hints.add(name)
    for item in all_items:
        if item.get("type") != "blob":
            continue
        file_path = str(item.get("path", ""))
        lower_name = file_path.rsplit("/", 1)[-1].lower()
        if lower_name == "dockerfile" or lower_name.startswith("dockerfile.") or lower_name.endswith(".dockerfile"):
            parent = file_path.rsplit("/", 1)[0] if "/" in file_path else "."
            parent_name = parent.rsplit("/", 1)[-1] if parent != "." else "root"
            if parent_name:
                hints.add(parent_name)
    return sorted(hints)


def _fetch_text_files_parallel(specs: list[tuple[str, str]], headers: dict, settings: dict) -> dict[str, str]:
    if not specs:
        return {}

    results: dict[str, str] = {}
    max_workers = min(settings["raw_max_workers"], len(specs))

    def _fetch_one(path: str, url: str) -> tuple[str, Optional[str]]:
        resp = _http_get(url, headers=headers, policy="raw", settings=settings)
        if resp and resp.status_code == 200:
            return path, resp.text[:10000]
        return path, None

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_path = {executor.submit(_fetch_one, path, url): path for path, url in specs}
        for future in as_completed(future_to_path):
            path = future_to_path[future]
            try:
                fetched_path, content = future.result()
                if content is not None:
                    results[fetched_path] = content
            except Exception:
                # Best-effort fetch: skip failures for individual files.
                continue
    return results


def fetch_repo_structure_impl(
    repo_url: str,
    github_token: Optional[str] = None,
    max_files: Optional[int] = 20,
    package_path: str = ".",
) -> dict:
    """Fetch repo metadata, file tree, and key file contents for deploy analysis."""
    settings = _settings()
    repo = repo_url.split("github.com/")[1].rstrip("/")
    headers = {"Authorization": f"token {github_token}"} if github_token else {}

    meta_resp = _http_get(f"https://api.github.com/repos/{repo}", headers=headers, policy="api", settings=settings)
    if not meta_resp:
        return {"error": "GitHub metadata fetch failed due to network timeout/retry exhaustion"}
    meta = meta_resp.json()
    if meta_resp.status_code == 404:
        if github_token:
            return {"error": "Repository not found or token lacks access"}
        return {"error": "Repository not found, or it is private and requires a GitHub token"}
    if meta_resp.status_code in (401, 403):
        return {"error": "GitHub API authentication failed or rate limit exceeded"}
    if "default_branch" not in meta:
        return {"error": f"Failed to fetch repository metadata: {meta.get('message', 'Unknown error')}"}

    normalized_package_path = _normalize_path(package_path)

    if normalized_package_path != ".":
        all_items, tree_error = _navigate_to_subtree(
            repo, meta["default_branch"], normalized_package_path, headers, settings
        )
        if tree_error:
            return {"error": tree_error}
    else:
        tree_resp = _http_get(
            f"https://api.github.com/repos/{repo}/git/trees/{meta['default_branch']}?recursive=1",
            headers=headers,
            policy="api",
            settings=settings,
        )
        if not tree_resp:
            return {"error": "Unable to fetch repository tree due to network timeout/retry exhaustion"}
        if tree_resp.status_code in (401, 403):
            return {"error": "Unable to fetch repository tree due to authentication/rate-limit restrictions"}
        if tree_resp.status_code != 200:
            return {"error": "Unable to fetch repository tree from GitHub"}
        all_items = tree_resp.json().get("tree", [])

    ref_resp = _http_get(
        f"https://api.github.com/repos/{repo}/git/ref/heads/{meta['default_branch']}",
        headers=headers,
        policy="api",
        settings=settings,
    )
    if not ref_resp:
        return {"error": "Unable to fetch repository ref due to network timeout/retry exhaustion"}
    ref_data = ref_resp.json()
    commit_sha = ref_data.get("object", {}).get("sha", "unknown")

    tree_entry_count = len(all_items)
    candidate_package_paths = _infer_candidate_package_paths(all_items)
    candidate_service_hints = _infer_candidate_service_hints(all_items, candidate_package_paths)

    fetch_specs: list[tuple[str, str]] = []
    if normalized_package_path != "." and settings["include_root_config_for_package"]:
        for root_file in ("package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "turbo.json", "pyproject.toml"):
            content_url = f"https://raw.githubusercontent.com/{repo}/{meta['default_branch']}/{root_file}"
            fetch_specs.append((root_file, content_url))

    for item in all_items:
        if item.get("type") != "blob":
            continue
        file_path = str(item.get("path", ""))
        if not _is_deploy_relevant_blob(file_path, normalized_package_path, settings["fetch_markdown"]):
            continue
        content_url = f"https://raw.githubusercontent.com/{repo}/{meta['default_branch']}/{file_path}"
        fetch_specs.append((file_path, content_url))

    # Deduplicate while preserving order.
    deduped_specs = list(dict.fromkeys(fetch_specs))
    limit = max_files if max_files is not None else 20
    if limit >= 0:
        deduped_specs = deduped_specs[:limit]
    key_files = _fetch_text_files_parallel(deduped_specs, headers, settings)

    result = {
        "repo_full_name": meta["full_name"],
        "default_branch": meta["default_branch"],
        "commit_sha": commit_sha,
        "language": meta.get("language"),
        "key_files": key_files,
        "dirs": [i["path"] for i in all_items if i.get("type") == "tree"][:20],
        "tree_entry_count": tree_entry_count,
        "candidate_package_paths": candidate_package_paths,
        "candidate_service_hints": candidate_service_hints,
    }
    return result


@tool(args_schema=RepoScanInput)
def fetch_repo_structure(
    repo_url: str,
    github_token: Optional[str] = None,
    max_files: Optional[int] = 20,
    package_path: str = ".",
) -> dict:
    """Fetch repo metadata, file tree, and key file contents for deploy analysis."""
    return fetch_repo_structure_impl(repo_url, github_token, max_files, package_path)
