from langchain_core.tools import tool
from pydantic import BaseModel, Field
from typing import Optional
import requests

class RepoScanInput(BaseModel):
    repo_url: str = Field(..., description="Full GitHub repo URL")
    github_token: Optional[str] = Field(None, description="Optional GitHub token (required for private repos)")
    max_files: Optional[int] = Field(20, description="Max files to analyze")
    package_path: str = Field(".", description="Sub-package path to analyze, '.' for entire repo")

@tool(args_schema=RepoScanInput)
def fetch_repo_structure(repo_url: str, github_token: Optional[str] = None, max_files: Optional[int] = 20, package_path: str = ".") -> dict:
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

    tree_resp = requests.get(
        f"https://api.github.com/repos/{repo}/git/trees/{meta['default_branch']}?recursive=1",
        headers=headers,
    )
    tree = tree_resp.json()
    if tree_resp.status_code in (401, 403):
        return {"error": "Unable to fetch repository tree due to authentication/rate-limit restrictions"}

    ref_resp = requests.get(
        f"https://api.github.com/repos/{repo}/git/ref/heads/{meta['default_branch']}",
        headers=headers,
    )
    ref_data = ref_resp.json()
    commit_sha = ref_data.get("object", {}).get("sha", "unknown")

    all_items = tree.get("tree", [])

    # Validate package_path exists if not root
    if package_path != ".":
        package_exists = any(
            item["type"] == "tree" and item["path"].rstrip("/") == package_path.rstrip("/")
            for item in all_items
        )
        if not package_exists:
            return {"error": f"Package path '{package_path}' not found in repository"}
        
        # Filter tree items to only those under package_path
        prefix = package_path.rstrip("/") + "/"
        all_items = [item for item in all_items if item["path"].startswith(prefix) or item["path"] == package_path.rstrip("/")]

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
        
        if item["type"] == "blob" and is_key_file:
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
