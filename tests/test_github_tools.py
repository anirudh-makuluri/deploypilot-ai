from tools.github_tools import fetch_repo_structure_impl


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_minimal_fetch_skips_markdown_by_default(monkeypatch):
    monkeypatch.delenv("SD_FETCH_MARKDOWN", raising=False)

    repo_api = "https://api.github.com/repos/owner/repo"
    tree_api = "https://api.github.com/repos/owner/repo/git/trees/main?recursive=1"
    ref_api = "https://api.github.com/repos/owner/repo/git/ref/heads/main"
    raw_root_readme = "https://raw.githubusercontent.com/owner/repo/main/README.md"
    raw_package_json = "https://raw.githubusercontent.com/owner/repo/main/package.json"

    responses = {
        repo_api: FakeResponse(200, {"full_name": "owner/repo", "default_branch": "main", "language": "TypeScript"}),
        tree_api: FakeResponse(
            200,
            {
                "tree": [
                    {"path": "README.md", "type": "blob"},
                    {"path": "package.json", "type": "blob"},
                ]
            },
        ),
        ref_api: FakeResponse(200, {"object": {"sha": "abc123"}}),
        raw_package_json: FakeResponse(200, text='{"name":"root-app"}'),
    }

    requested_urls = []

    def fake_get(url, headers=None):
        requested_urls.append(url)
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

    assert result["commit_sha"] == "abc123"
    assert "package.json" in result["key_files"]
    assert "README.md" not in result["key_files"]
    assert raw_root_readme not in requested_urls


def test_minimal_fetch_includes_required_deploy_files(monkeypatch):
    monkeypatch.delenv("SD_FETCH_MARKDOWN", raising=False)

    repo_api = "https://api.github.com/repos/owner/repo"
    tree_api = "https://api.github.com/repos/owner/repo/git/trees/main?recursive=1"
    ref_api = "https://api.github.com/repos/owner/repo/git/ref/heads/main"
    raw_pkg = "https://raw.githubusercontent.com/owner/repo/main/package.json"
    raw_reqs = "https://raw.githubusercontent.com/owner/repo/main/requirements.txt"
    raw_compose = "https://raw.githubusercontent.com/owner/repo/main/docker-compose.yml"
    raw_nginx = "https://raw.githubusercontent.com/owner/repo/main/nginx.conf"

    responses = {
        repo_api: FakeResponse(200, {"full_name": "owner/repo", "default_branch": "main", "language": "Python"}),
        tree_api: FakeResponse(
            200,
            {
                "tree": [
                    {"path": "package.json", "type": "blob"},
                    {"path": "requirements.txt", "type": "blob"},
                    {"path": "docker-compose.yml", "type": "blob"},
                    {"path": "nginx.conf", "type": "blob"},
                    {"path": "README.md", "type": "blob"},
                    {"path": "apps", "type": "tree"},
                    {"path": "apps/web", "type": "tree"},
                    {"path": "apps/web/package.json", "type": "blob"},
                ]
            },
        ),
        ref_api: FakeResponse(200, {"object": {"sha": "rootsha"}}),
        raw_pkg: FakeResponse(200, text='{"name":"root"}'),
        raw_reqs: FakeResponse(200, text="fastapi==0.116.0"),
        raw_compose: FakeResponse(200, text="services:\n  api:\n    build: .\n"),
        raw_nginx: FakeResponse(200, text="events {}\nhttp {}"),
        "https://raw.githubusercontent.com/owner/repo/main/apps/web/package.json": FakeResponse(200, text='{"name":"web"}'),
    }

    def fake_get(url, headers=None):
        if url not in responses:
            raise AssertionError(f"Unexpected URL requested: {url}")
        return responses[url]

    monkeypatch.setattr("tools.github_tools.requests.get", fake_get)

    result = fetch_repo_structure_impl(
        repo_url="https://github.com/owner/repo",
        github_token="token",
        max_files=50,
        package_path=".",
    )

    assert "package.json" in result["key_files"]
    assert "requirements.txt" in result["key_files"]
    assert "docker-compose.yml" in result["key_files"]
    assert "nginx.conf" in result["key_files"]
    assert "README.md" not in result["key_files"]
    assert result["tree_entry_count"] == 8
    assert result["candidate_package_paths"] == [".", "apps/web"]
    assert "web" in result["candidate_service_hints"]
