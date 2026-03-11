from tools.github_tools import fetch_repo_structure_impl


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_fetch_repo_structure_includes_relevant_markdown_docs(monkeypatch):
    # For non-root package_path, the implementation now uses targeted subtree navigation
    # instead of a full recursive tree fetch (avoids truncation on large monorepos).
    repo_api = "https://api.github.com/repos/owner/repo"
    root_tree_api = "https://api.github.com/repos/owner/repo/git/trees/main"
    apps_tree_api = "https://api.github.com/repos/owner/repo/git/trees/apps-sha"
    web_subtree_api = "https://api.github.com/repos/owner/repo/git/trees/web-sha?recursive=1"
    ref_api = "https://api.github.com/repos/owner/repo/git/ref/heads/main"
    raw_package_readme = "https://raw.githubusercontent.com/owner/repo/main/apps/web/README.md"
    raw_package_deploy = "https://raw.githubusercontent.com/owner/repo/main/apps/web/deployment.md"
    raw_package_json = "https://raw.githubusercontent.com/owner/repo/main/apps/web/package.json"

    responses = {
        repo_api: FakeResponse(200, {"full_name": "owner/repo", "default_branch": "main", "language": "TypeScript"}),
        # Step 1: root tree (non-recursive) — just needs the 'apps' directory entry
        root_tree_api: FakeResponse(200, {"tree": [
            {"path": "README.md", "type": "blob"},
            {"path": "docs", "type": "tree", "sha": "docs-sha"},
            {"path": "apps", "type": "tree", "sha": "apps-sha"},
        ]}),
        # Step 2: apps-level tree (non-recursive) — needs the 'web' directory entry
        apps_tree_api: FakeResponse(200, {"tree": [
            {"path": "web", "type": "tree", "sha": "web-sha"},
        ]}),
        # Step 3: web subtree (recursive) — paths are relative to apps/web/
        web_subtree_api: FakeResponse(200, {"tree": [
            {"path": "README.md", "type": "blob"},
            {"path": "deployment.md", "type": "blob"},
            {"path": "docs", "type": "tree"},
            {"path": "docs/notes.md", "type": "blob"},
            {"path": "package.json", "type": "blob"},
        ]}),
        ref_api: FakeResponse(200, {"object": {"sha": "abc123"}}),
        raw_package_readme: FakeResponse(200, text="# Package README"),
        raw_package_deploy: FakeResponse(200, text="# Deploy"),
        raw_package_json: FakeResponse(200, text='{"name":"web"}'),
    }

    def fake_get(url, headers=None):
        if url not in responses:
            raise AssertionError(f"Unexpected URL requested: {url}")
        return responses[url]

    monkeypatch.setattr("tools.github_tools.requests.get", fake_get)

    result = fetch_repo_structure_impl(
        repo_url="https://github.com/owner/repo",
        github_token="token",
        max_files=10,
        package_path="apps/web",
    )

    assert result["commit_sha"] == "abc123"
    assert "apps/web/README.md" in result["key_files"]
    assert "apps/web/deployment.md" in result["key_files"]
    assert "apps/web/package.json" in result["key_files"]
    assert "README.md" not in result["key_files"]
    assert "docs/architecture.md" not in result["key_files"]
    assert "apps/web/docs/notes.md" not in result["key_files"]


def test_fetch_repo_structure_includes_root_markdown_docs_for_root_package(monkeypatch):
    repo_api = "https://api.github.com/repos/owner/repo"
    tree_api = "https://api.github.com/repos/owner/repo/git/trees/main?recursive=1"
    ref_api = "https://api.github.com/repos/owner/repo/git/ref/heads/main"
    raw_root_readme = "https://raw.githubusercontent.com/owner/repo/main/README.md"
    raw_root_setup = "https://raw.githubusercontent.com/owner/repo/main/setup.md"
    raw_package_json = "https://raw.githubusercontent.com/owner/repo/main/package.json"

    responses = {
        repo_api: FakeResponse(200, {"full_name": "owner/repo", "default_branch": "main", "language": "TypeScript"}),
        tree_api: FakeResponse(
            200,
            {
                "tree": [
                    {"path": "README.md", "type": "blob"},
                    {"path": "setup.md", "type": "blob"},
                    {"path": "docs/overview.md", "type": "blob"},
                    {"path": "package.json", "type": "blob"},
                ]
            },
        ),
        ref_api: FakeResponse(200, {"object": {"sha": "rootsha"}}),
        raw_root_readme: FakeResponse(200, text="# Root README"),
        raw_root_setup: FakeResponse(200, text="# Setup"),
        raw_package_json: FakeResponse(200, text='{"name":"root-app"}'),
    }

    def fake_get(url, headers=None):
        if url not in responses:
            raise AssertionError(f"Unexpected URL requested: {url}")
        return responses[url]

    monkeypatch.setattr("tools.github_tools.requests.get", fake_get)

    result = fetch_repo_structure_impl(
        repo_url="https://github.com/owner/repo",
        github_token="token",
        max_files=10,
        package_path=".",
    )

    assert "README.md" in result["key_files"]
    assert "setup.md" in result["key_files"]
    assert "package.json" in result["key_files"]
    assert "docs/overview.md" not in result["key_files"]