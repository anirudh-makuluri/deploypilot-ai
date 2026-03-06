from langchain_core.tools import tool
from pydantic import BaseModel, Field
from typing import Optional
import requests

class RepoScanInput(BaseModel):
    repo_url: str = Field(..., description="Full GitHub repo URL")
    github_token: str = Field(..., description="User's GitHub token")
    max_files: Optional[int] = Field(20, description="Max files to analyze")

@tool(args_schema=RepoScanInput)
def fetch_repo_structure(repo_url: str, github_token: str, max_files: Optional[int] = 20) -> dict:
    """Fetch repo metadata, file tree, and key file contents for deploy analysis."""
    print("===============================================")
    print(f"repo_url: {repo_url}")
    repo = repo_url.split("github.com/")[1].rstrip("/")

    headers = {"Authorization": f"token {github_token}"}

    meta = requests.get(f"https://api.github.com/repos/{repo}", headers=headers).json()
    if "message" in meta and meta["message"] == "Not Found":
         return {"error": "Repository not found or token invalid"}

    tree = requests.get(
        f"https://api.github.com/repos/{repo}/git/trees/{meta['default_branch']}?recursive=1",
        headers=headers,
    ).json()

    key_paths = [
        "package.json",
        "requirements.txt",
        "pnpm-lock.yaml",
        "Dockerfile",
        "docker-compose.yml",
    ]
    key_files = {}

    count = 0
    limit = max_files if max_files is not None else 20
    for item in tree.get("tree", []):
        if count >= limit:
            break
        if item["type"] == "blob" and item["path"] in key_paths:
            content_url = f"https://raw.githubusercontent.com/{repo}/{meta['default_branch']}/{item['path']}"
            key_files[item["path"]] = requests.get(content_url, headers=headers).text[:4000]
            count += 1

    result = {
        "repo_full_name": meta["full_name"],
        "default_branch": meta["default_branch"],
        "language": meta.get("language"),
        "stargazers_count": meta.get("stargazers_count", 0),
        "key_files": key_files,
        "dirs": [i["path"] for i in tree.get("tree", []) if i["type"] == "tree"][:20],
    }
    return result
