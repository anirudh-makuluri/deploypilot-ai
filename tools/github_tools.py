from langchain_core.tools import tool
from pydantic import BaseModel, Field
from typing import Optional
import requests


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

def _navigate_to_subtree(repo: str, branch: str, package_path: str, headers: dict):
    """Walk path components to the target directory and fetch its subtree recursively.

    Uses N non-recursive tree API calls (one per path depth level) to locate the
    directory SHA, then one recursive fetch of just that subtree.  This avoids the
    GitHub 100k-entry truncation that happens when doing a full-repo recursive fetch
    on large monorepos like vercel/next.js.

    Args:
        package_path: Already-normalized path (forward slashes, no leading/trailing slash).

    Returns:
        (items, error_message) where items have paths prefixed with package_path.
        error_message is None on success.
    """
    components = [c for c in package_path.split("/") if c]
    current_ref = branch

    for component in components:
        level_resp = requests.get(
            f"https://api.github.com/repos/{repo}/git/trees/{current_ref}",
            headers=headers,
        )
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

    subtree_resp = requests.get(
        f"https://api.github.com/repos/{repo}/git/trees/{current_ref}?recursive=1",
        headers=headers,
    )
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


def fetch_repo_structure_impl(repo_url: str, github_token: Optional[str] = None, max_files: Optional[int] = 20, package_path: str = ".") -> dict:
    """Fetch repo metadata, file tree, and key file contents for deploy analysis."""
    repo = repo_url.split("github.com/")[1].rstrip("/")

    headers = {"Authorization": f"token {github_token}"} if github_token else {}

    meta_resp = requests.get(f"https://api.github.com/repos/{repo}", headers=headers)
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
        # Use targeted subtree navigation for sub-package paths.
        # This avoids the 100k-entry truncation on large monorepos (e.g. vercel/next.js)
        # by fetching only the subtree rooted at the requested directory.
        all_items, tree_error = _navigate_to_subtree(
            repo, meta["default_branch"], normalized_package_path, headers
        )
        if tree_error:
            return {"error": tree_error}
    else:
        tree_resp = requests.get(
            f"https://api.github.com/repos/{repo}/git/trees/{meta['default_branch']}?recursive=1",
            headers=headers,
        )
        if tree_resp.status_code in (401, 403):
            return {"error": "Unable to fetch repository tree due to authentication/rate-limit restrictions"}
        all_items = tree_resp.json().get("tree", [])

    ref_resp = requests.get(
        f"https://api.github.com/repos/{repo}/git/ref/heads/{meta['default_branch']}",
        headers=headers,
    )
    ref_data = ref_resp.json()
    commit_sha = ref_data.get("object", {}).get("sha", "unknown")

    key_filenames = [
        "package.json",
        "requirements.txt",
        "pnpm-lock.yaml",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "nginx.conf",
    ]
    key_files = {}

    # When scanning a sub-package, also fetch root-level config files that are
    # critical for monorepo/turbo detection. These aren't in the subtree.
    if normalized_package_path != ".":
        root_config_files = [
            "package.json",
            "pnpm-lock.yaml",
            "pnpm-workspace.yaml",
            "turbo.json",
        ]
        for root_file in root_config_files:
            content_url = f"https://raw.githubusercontent.com/{repo}/{meta['default_branch']}/{root_file}"
            try:
                resp = requests.get(content_url, headers=headers)
                if resp.status_code == 200:
                    key_files[root_file] = resp.text[:10000]
            except Exception:
                pass

    count = 0
    limit = max_files if max_files is not None else 20
    for item in all_items:
        if count >= limit:
            break
        
        path_name = item["path"].split("/")[-1]
        is_key_file = (
            path_name in key_filenames or 
            path_name.startswith("Dockerfile.") or 
            path_name.endswith(".Dockerfile")
        )
        is_relevant_markdown = _is_relevant_markdown_file(item["path"], package_path)
        
        if item["type"] == "blob" and (is_key_file or is_relevant_markdown):
            content_url = f"https://raw.githubusercontent.com/{repo}/{meta['default_branch']}/{item['path']}"
            key_files[item["path"]] = requests.get(content_url, headers=headers).text[:10000]
            count += 1

    result = {
        "repo_full_name": meta["full_name"],
        "default_branch": meta["default_branch"],
        "commit_sha": commit_sha,
        "language": meta.get("language"),
        "key_files": key_files,
        "dirs": [i["path"] for i in all_items if i["type"] == "tree"][:20],
    }
    return result

@tool(args_schema=RepoScanInput)
def fetch_repo_structure(repo_url: str, github_token: Optional[str] = None, max_files: Optional[int] = 20, package_path: str = ".") -> dict:
    """Fetch repo metadata, file tree, and key file contents for deploy analysis."""
    return fetch_repo_structure_impl(repo_url, github_token, max_files, package_path)
