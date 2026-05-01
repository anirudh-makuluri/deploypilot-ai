import json

from fastapi.testclient import TestClient

import app as app_module
import db as db_module


class FakeTracker:
    def __init__(self):
        self.usage = {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}

    def get_usage(self):
        return self.usage


class FakeExecuteResponse:
    def __init__(self, data):
        self.data = data


class FakeTableQuery:
    def __init__(self, supabase, table_name):
        self.supabase = supabase
        self.table_name = table_name
        self.operation = None
        self.insert_payload = None
        self.upsert_payload = None
        self.filters = {}
        self.expect_single = False

    def select(self, _columns):
        self.operation = "select"
        return self

    def delete(self):
        self.operation = "delete"
        return self

    def insert(self, payload):
        self.operation = "insert"
        self.insert_payload = payload
        return self

    def upsert(self, payload, on_conflict=None):
        self.operation = "upsert"
        self.upsert_payload = payload
        self.upsert_on_conflict = on_conflict
        return self

    def update(self, payload):
        self.operation = "update"
        self.update_payload = payload
        return self

    def single(self):
        self.expect_single = True
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def is_(self, key, value):
        # Supabase uses IS for NULL checks. For the fake, treat it as equality to None.
        self.filters[key] = value
        return self

    def execute(self):
        if self.supabase.fail_on_execute:
            raise RuntimeError("forced execute failure")

        if self.operation == "insert":
            if self.table_name == "analysis_cache":
                self.supabase.insert_attempts += 1
                if self.supabase.fail_insert_attempts > 0:
                    self.supabase.fail_insert_attempts -= 1
                    raise RuntimeError("forced insert failure")
                self.supabase.inserted_payloads.append(self.insert_payload)
                self.supabase.cache_rows.append(
                    {
                        "id": f"id-{len(self.supabase.cache_rows) + 1}",
                        "repo_url": self.insert_payload.get("repo_url"),
                        "response_id": self.insert_payload.get("response_id"),
                        "commit_sha": self.insert_payload.get("commit_sha"),
                        "package_path": self.insert_payload.get("package_path"),
                        "service_name": self.insert_payload.get("service_name"),
                        "result": self.insert_payload.get("result"),
                    }
                )
            elif self.table_name == "analysis_responses":
                self.supabase.response_log_payloads.append(self.insert_payload)
                row = dict(self.insert_payload)
                if not row.get("id"):
                    row["id"] = f"resp-{len(self.supabase.response_rows) + 1}"
                self.supabase.response_rows.append(row)
            return FakeExecuteResponse([self.insert_payload])

        if self.operation == "upsert":
            self.supabase.upserted_payloads.append(self.upsert_payload)
            if self.table_name == "analysis_cache":
                matched = False
                for row in self.supabase.cache_rows:
                    if (
                        row.get("repo_url") == self.upsert_payload.get("repo_url")
                        and row.get("commit_sha") == self.upsert_payload.get("commit_sha")
                        and row.get("package_path") == self.upsert_payload.get("package_path")
                        and row.get("service_name") == self.upsert_payload.get("service_name")
                    ):
                        row["result"] = self.upsert_payload.get("result")
                        matched = True
                        break
                if not matched:
                    self.supabase.cache_rows.append(
                        {
                            "id": f"id-{len(self.supabase.cache_rows) + 1}",
                            "repo_url": self.upsert_payload.get("repo_url"),
                            "response_id": self.upsert_payload.get("response_id"),
                            "commit_sha": self.upsert_payload.get("commit_sha"),
                            "package_path": self.upsert_payload.get("package_path"),
                            "service_name": self.upsert_payload.get("service_name"),
                            "result": self.upsert_payload.get("result"),
                        }
                    )
            return FakeExecuteResponse([self.upsert_payload])

        if self.operation == "update":
            if self.table_name == "analysis_responses":
                updated = []
                for row in self.supabase.response_rows:
                    if self._matches(row):
                        row.update(self.update_payload or {})
                        updated.append(row)
                return FakeExecuteResponse(updated)
            return FakeExecuteResponse([])

        if self.operation == "select":
            source = self.supabase.cache_rows if self.table_name == "analysis_cache" else self.supabase.response_rows
            rows = [row for row in source if self._matches(row)]
            if self.expect_single:
                if len(rows) != 1:
                    raise RuntimeError("single row not found")
                return FakeExecuteResponse(rows[0])
            return FakeExecuteResponse(rows)

        if self.operation == "delete":
            if self.table_name == "analysis_cache":
                removed = [row for row in self.supabase.cache_rows if self._matches(row)]
                self.supabase.cache_rows = [row for row in self.supabase.cache_rows if not self._matches(row)]
            else:
                removed = [row for row in self.supabase.response_rows if self._matches(row)]
                self.supabase.response_rows = [row for row in self.supabase.response_rows if not self._matches(row)]
            return FakeExecuteResponse(removed)

        return FakeExecuteResponse([])

    def _matches(self, row):
        for key, value in self.filters.items():
            if row.get(key) != value:
                return False
        return True


class FakeSupabase:
    def __init__(self, cache_rows=None, fail_insert_attempts=0, fail_on_execute=False):
        self.cache_rows = cache_rows or []
        self.fail_insert_attempts = fail_insert_attempts
        self.fail_on_execute = fail_on_execute
        self.insert_attempts = 0
        self.inserted_payloads = []
        self.response_log_payloads = []
        self.response_rows = []
        self.upserted_payloads = []

    def table(self, table_name):
        return FakeTableQuery(self, table_name)


def _set_common_mocks(monkeypatch):
    monkeypatch.setattr(app_module, "TokenTracker", FakeTracker)

def _set_auth(monkeypatch):
    monkeypatch.setenv("SD_API_BEARER_TOKEN", "test-token")

def _auth_headers():
    return {"Authorization": "Bearer test-token"}


def _client():
    return TestClient(app_module.app)


def _parse_sse(response_text):
    events = []
    for block in response_text.strip().split("\n\n"):
        if not block.strip():
            continue
        event_name = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line.replace("event: ", "", 1).strip()
            if line.startswith("data: "):
                payload = line.replace("data: ", "", 1)
                data = json.loads(payload)
        if event_name:
            events.append((event_name, data))
    return events


def test_analyze_returns_400_on_graph_error(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)
    monkeypatch.setattr(app_module.graph, "invoke", lambda *_args, **_kwargs: {"error": "scan failed"})

    response = _client().post("/analyze", json={"repo_url": "https://github.com/acme/repo"}, headers=_auth_headers())

    assert response.status_code == 400
    assert response.json()["detail"] == "scan failed"


def test_analyze_returns_400_on_preflight_error(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)
    monkeypatch.setattr(
        app_module.graph,
        "invoke",
        lambda *_args, **_kwargs: {
            "error": {
                "code": "preflight_failed",
                "message": "Static preflight checks failed for generated Dockerfiles.",
                "issues": ["web: COPY source '../package.json' escapes build context 'apps/web'"],
            }
        },
    )

    response = _client().post("/analyze", json={"repo_url": "https://github.com/acme/repo"}, headers=_auth_headers())

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "preflight_failed"


def test_health_endpoint_reports_basic_configuration(monkeypatch):
    monkeypatch.setenv("SD_API_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(db_module, "supabase", FakeSupabase())

    response = _client().get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "scope": "public",
        "supabase_configured": True,
    }


def test_authenticated_health_endpoint_requires_valid_token(monkeypatch):
    _set_auth(monkeypatch)
    monkeypatch.setattr(db_module, "supabase", FakeSupabase())

    unauthorized = _client().get("/healthz")
    authorized = _client().get("/healthz", headers=_auth_headers())

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.json() == {
        "status": "ok",
        "scope": "authenticated",
        "supabase_configured": True,
    }


def test_analyze_returns_cached_payload_with_commit_sha_backfill(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)
    monkeypatch.setattr(
        app_module.graph,
        "invoke",
        lambda *_args, **_kwargs: {
            "commit_sha": "abc123",
            "cached_response": {
                "stack_summary": "Python",
                "services": [],
                "dockerfiles": {},
                "risks": [],
                "confidence": 0.9,
            },
        },
    )

    response = _client().post("/analyze", json={"repo_url": "https://github.com/acme/repo"}, headers=_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["commit_sha"] == "abc123"
    assert payload["stack_summary"] == "Python"


def test_analyze_success_without_supabase(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)
    monkeypatch.setattr(
        app_module.graph,
        "invoke",
        lambda *_args, **_kwargs: {
            "commit_sha": "sha-1",
            "detected_stack": "FastAPI",
            "services": [{"name": "api", "build_context": ".", "port": 8000}],
            "dockerfiles": {"Dockerfile": "FROM python:3.11"},
            "risks": ["none"],
            "confidence": 0.8,
        },
    )
    monkeypatch.setattr(db_module, "supabase", None)

    response = _client().post(
        "/analyze",
        json={"repo_url": "https://github.com/acme/repo", "package_path": "services/api"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["response_id"]
    assert payload["commit_sha"] == "sha-1"
    assert payload["token_usage"]["total_tokens"] == 18


def test_analyze_success_caches_result(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)
    fake_supabase = FakeSupabase()
    monkeypatch.setattr(db_module, "supabase", fake_supabase)
    monkeypatch.setattr(
        app_module.graph,
        "invoke",
        lambda *_args, **_kwargs: {
            "commit_sha": "sha-cache",
            "detected_stack": "Node",
            "services": [],
            "dockerfiles": {},
            "risks": [],
            "confidence": 0.7,
        },
    )

    response = _client().post(
        "/analyze",
        json={"repo_url": "https://github.com/acme/repo", "package_path": "apps/web"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert len(fake_supabase.inserted_payloads) == 1
    inserted = fake_supabase.inserted_payloads[0]
    assert inserted["repo_url"] == "https://github.com/acme/repo"
    assert inserted["commit_sha"] == "sha-cache"
    assert inserted["response_id"]
    assert inserted["result"]["_cache_package_path"] == "apps/web"
    assert len(fake_supabase.response_log_payloads) == 1
    assert fake_supabase.response_log_payloads[0]["endpoint"] == "/analyze"


def test_analyze_cache_insert_retries_until_success(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)
    fake_supabase = FakeSupabase(fail_insert_attempts=2)
    monkeypatch.setattr(db_module, "supabase", fake_supabase)
    monkeypatch.setattr(app_module.graph, "invoke", lambda *_args, **_kwargs: {"commit_sha": "sha", "risks": [], "confidence": 0.5})
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    response = _client().post("/analyze", json={"repo_url": "https://github.com/acme/repo"}, headers=_auth_headers())

    assert response.status_code == 200
    assert fake_supabase.insert_attempts == 3
    assert len(fake_supabase.inserted_payloads) == 1


def test_examples_seed_success(monkeypatch):
    _set_auth(monkeypatch)
    called = {}

    def fake_seed(**kwargs):
        called.update(kwargs)
        return {"inserted": 1, "updated": 2, "skipped": 3, "errors": []}

    monkeypatch.setattr(app_module, "seed_example_bank_from_repos", fake_seed)

    response = _client().post(
        "/examples/seed",
        json={
            "repo_urls": ["https://github.com/acme/repo"],
            "github_token": "ghs_x",
            "max_files_per_repo": 9,
            "permissive_only": False,
        },
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {"inserted": 1, "updated": 2, "skipped": 3, "errors": []}
    assert called["repo_urls"] == ["https://github.com/acme/repo"]
    assert called["github_token"] == "ghs_x"
    assert called["max_files_per_repo"] == 9
    assert called["permissive_only"] is False


def test_examples_seed_popular_uses_builtin_list(monkeypatch):
    _set_auth(monkeypatch)
    called = {}

    def fake_seed(**kwargs):
        called.update(kwargs)
        return {"inserted": 0, "updated": 0, "skipped": 1, "errors": []}

    monkeypatch.setattr(app_module, "seed_example_bank_from_repos", fake_seed)
    monkeypatch.setattr(app_module, "POPULAR_EXAMPLE_REPOS", ["https://github.com/acme/one"])

    response = _client().post("/examples/seed/popular?github_token=mytoken", headers=_auth_headers())

    assert response.status_code == 200
    assert called["repo_urls"] == ["https://github.com/acme/one"]
    assert called["github_token"] == "mytoken"
    assert called["max_files_per_repo"] == 20
    assert called["permissive_only"] is True


def test_examples_preview_requires_auth(monkeypatch):
    _set_auth(monkeypatch)

    response = _client().post(
        "/examples/preview",
        json={"artifact_type": "dockerfile", "detected_stack": "FastAPI", "limit": 2},
    )

    assert response.status_code == 401


def test_examples_preview_rejects_invalid_artifact_type(monkeypatch):
    _set_auth(monkeypatch)

    response = _client().post(
        "/examples/preview",
        json={"artifact_type": "nginx", "detected_stack": "FastAPI", "limit": 2},
        headers=_auth_headers(),
    )

    assert response.status_code == 400
    assert "artifact_type must be" in response.json()["detail"]


def test_examples_preview_success(monkeypatch):
    _set_auth(monkeypatch)
    called = {}

    def fake_fetch(**kwargs):
        called.update(kwargs)
        return [{"source_repo": "acme/repo", "snippet": "FROM python"}]

    monkeypatch.setattr(app_module, "fetch_reference_examples", fake_fetch)

    response = _client().post(
        "/examples/preview",
        json={
            "artifact_type": "dockerfile",
            "detected_stack": "FastAPI",
            "service": {"name": "api", "build_context": "."},
            "limit": 2,
        },
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json()["examples"][0]["source_repo"] == "acme/repo"
    assert called["artifact_type"] == "dockerfile"
    assert called["detected_stack"] == "FastAPI"
    assert called["stack_tokens"] == []
    assert called["service"] == {"name": "api", "build_context": "."}
    assert called["limit"] == 2


def test_delete_cache_returns_503_when_supabase_missing(monkeypatch):
    _set_auth(monkeypatch)
    monkeypatch.setattr(db_module, "supabase", None)

    response = _client().request("DELETE", "/cache", json={"repo_url": "https://github.com/acme/repo"}, headers=_auth_headers())

    assert response.status_code == 503
    assert "Supabase is not configured" in response.json()["detail"]


def test_delete_cache_returns_404_when_not_found(monkeypatch):
    _set_auth(monkeypatch)
    monkeypatch.setattr(db_module, "supabase", FakeSupabase(cache_rows=[]))

    response = _client().request(
        "DELETE",
        "/cache",
        json={"repo_url": "https://github.com/acme/repo", "commit_sha": "x"},
        headers=_auth_headers(),
    )

    assert response.status_code == 404


def test_delete_cache_with_commit_sha_success(monkeypatch):
    _set_auth(monkeypatch)
    monkeypatch.setattr(
        db_module,
        "supabase",
        FakeSupabase(
            cache_rows=[
                {"id": "1", "repo_url": "https://github.com/acme/repo", "commit_sha": "a", "package_path": ".", "service_name": None, "result": {}},
                {"id": "2", "repo_url": "https://github.com/acme/repo", "commit_sha": "b", "package_path": ".", "service_name": None, "result": {}},
            ]
        ),
    )

    response = _client().request(
        "DELETE",
        "/cache",
        json={"repo_url": "https://github.com/acme/repo", "commit_sha": "b"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {
        "deleted": 1,
        "repo_url": "https://github.com/acme/repo",
        "commit_sha": "b",
        "package_path": ".",
        "service_name": None,
    }


def test_delete_cache_by_repo_success(monkeypatch):
    _set_auth(monkeypatch)
    fake_supabase = FakeSupabase(
        cache_rows=[
            {"id": "1", "repo_url": "https://github.com/acme/repo", "commit_sha": "a", "package_path": ".", "service_name": None, "result": {}},
            {"id": "2", "repo_url": "https://github.com/acme/repo", "commit_sha": "b", "package_path": ".", "service_name": None, "result": {}},
            {"id": "3", "repo_url": "https://github.com/acme/other", "commit_sha": "z", "package_path": ".", "service_name": None, "result": {}},
        ]
    )
    monkeypatch.setattr(db_module, "supabase", fake_supabase)

    response = _client().request("DELETE", "/cache", json={"repo_url": "https://github.com/acme/repo"}, headers=_auth_headers())

    assert response.status_code == 200
    assert response.json()["deleted"] == 2
    assert len(fake_supabase.cache_rows) == 1


def test_delete_cache_returns_500_for_unexpected_failure(monkeypatch):
    _set_auth(monkeypatch)
    monkeypatch.setattr(db_module, "supabase", FakeSupabase(fail_on_execute=True))

    response = _client().request("DELETE", "/cache", json={"repo_url": "https://github.com/acme/repo"}, headers=_auth_headers())

    assert response.status_code == 500
    assert "Failed to delete cache" in response.json()["detail"]


def test_analyze_stream_emits_error_event_from_node(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)

    async def fake_astream(_initial_state, config=None):
        callbacks = config.get("callbacks", []) if config else []
        assert len(callbacks) == 1
        yield {"scanner": {"error": "scanner failed"}}

    monkeypatch.setattr(app_module.graph, "astream", fake_astream)

    response = _client().post("/analyze/stream", json={"repo_url": "https://github.com/acme/repo"}, headers=_auth_headers())

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert events[0][0] == "progress"
    assert events[1][0] == "error"
    assert events[1][1]["detail"] == "scanner failed"


def test_analyze_stream_emits_preflight_error_event(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)

    async def fake_astream(_initial_state, config=None):
        callbacks = config.get("callbacks", []) if config else []
        assert len(callbacks) == 1
        yield {"scanner": {"commit_sha": "sha-stream"}}
        yield {
            "preflight": {
                "preflight_issues": ["web: COPY source '../package.json' escapes build context 'apps/web'"],
                "error": {
                    "code": "preflight_failed",
                    "message": "Static preflight checks failed for generated Dockerfiles.",
                    "issues": ["web: COPY source '../package.json' escapes build context 'apps/web'"],
                },
            }
        }

    monkeypatch.setattr(app_module.graph, "astream", fake_astream)

    response = _client().post("/analyze/stream", json={"repo_url": "https://github.com/acme/repo"}, headers=_auth_headers())

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert events[0][0] == "progress"
    assert events[1][0] == "progress"
    assert events[2][0] == "error"
    assert events[2][1]["detail"]["code"] == "preflight_failed"


def test_analyze_stream_emits_cached_complete_and_backfills_fields(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)

    async def fake_astream(_initial_state, config=None):
        callbacks = config.get("callbacks", []) if config else []
        assert len(callbacks) == 1
        yield {
            "scanner": {
                "commit_sha": "stream-sha",
                "cached_response": {
                    "stack_summary": "Python",
                    "services": [],
                    "dockerfiles": {},
                    "risks": [],
                    "confidence": 0.88,
                },
            }
        }

    monkeypatch.setattr(app_module.graph, "astream", fake_astream)

    response = _client().post("/analyze/stream", json={"repo_url": "https://github.com/acme/repo"}, headers=_auth_headers())

    events = _parse_sse(response.text)
    # Cache hits should still look like a full run to clients.
    progress_nodes = [data["node"] for (name, data) in events if name == "progress"]
    assert progress_nodes[:1] == ["scanner"]
    assert "planner" in progress_nodes
    assert "docker_gen" in progress_nodes
    assert events[-1][0] == "complete"
    assert events[-1][1]["commit_sha"] == "stream-sha"
    assert events[-1][1]["token_usage"]["total_tokens"] == 18


def test_analyze_stream_success_caches_and_completes(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)
    fake_supabase = FakeSupabase()
    monkeypatch.setattr(db_module, "supabase", fake_supabase)

    async def fake_astream(_initial_state, config=None):
        callbacks = config.get("callbacks", []) if config else []
        assert len(callbacks) == 1
        yield {"scanner": {"commit_sha": "sha-stream"}}
        yield {
            "planner": {
                "detected_stack": "FastAPI",
                "services": [{"name": "api", "build_context": ".", "port": 8000}],
                "dockerfiles": {"Dockerfile": "FROM python:3.11"},
                "risks": ["none"],
                "confidence": 0.9,
            }
        }

    monkeypatch.setattr(app_module.graph, "astream", fake_astream)

    response = _client().post(
        "/analyze/stream",
        json={"repo_url": "https://github.com/acme/repo", "package_path": "services/api"},
        headers=_auth_headers(),
    )

    events = _parse_sse(response.text)
    assert events[-1][0] == "complete"
    assert events[-1][1]["response_id"]
    assert events[-1][1]["commit_sha"] == "sha-stream"
    assert events[-1][1]["token_usage"]["total_tokens"] == 18
    assert len(fake_supabase.inserted_payloads) == 1
    assert fake_supabase.inserted_payloads[0]["result"]["_cache_package_path"] == "services/api"
    assert len(fake_supabase.response_log_payloads) == 1
    assert fake_supabase.response_log_payloads[0]["endpoint"] == "/analyze/stream"


def test_response_status_false_updates_and_deletes_cache(monkeypatch):
    _set_auth(monkeypatch)
    fake_supabase = FakeSupabase(
        cache_rows=[
            {
                "id": "cache-1",
                "repo_url": "https://github.com/acme/repo",
                "commit_sha": "sha-1",
                "package_path": ".",
                "service_name": None,
                "result": {"ok": True},
            }
        ]
    )
    fake_supabase.response_rows = [
        {
            "id": "resp-1",
            "repo_url": "https://github.com/acme/repo",
            "commit_sha": "sha-1",
            "package_path": ".",
            "service_name": None,
            "passed": False,
            "payload": {"ok": True},
        }
    ]
    monkeypatch.setattr(db_module, "supabase", fake_supabase)

    response = _client().post(
        "/responses/status",
        json={"response_id": "resp-1", "passed": False},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["response_id"] == "resp-1"
    assert payload["passed"] is False
    assert payload["cache_deleted"] == 1
    assert fake_supabase.response_rows[0]["passed"] is False
    assert fake_supabase.cache_rows == []


def test_analyze_stream_emits_error_for_top_level_exception(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)

    async def fake_astream(_initial_state, config=None):
        callbacks = config.get("callbacks", []) if config else []
        assert len(callbacks) == 1
        raise RuntimeError("boom")
        yield

    monkeypatch.setattr(app_module.graph, "astream", fake_astream)

    response = _client().post("/analyze/stream", json={"repo_url": "https://github.com/acme/repo"}, headers=_auth_headers())

    events = _parse_sse(response.text)
    assert len(events) == 1
    assert events[0][0] == "error"
    assert "boom" in events[0][1]["detail"]

def test_analyze_rejects_unauthenticated_even_for_cached_commit(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)
    monkeypatch.setattr(
        db_module,
        "supabase",
        FakeSupabase(
            cache_rows=[
                {
                    "id": "1",
                    "repo_url": "https://github.com/acme/repo",
                    "commit_sha": "sha-cached",
                    "package_path": ".",
                    "service_name": None,
                    "result": {
                        "commit_sha": "sha-cached",
                        "stack_summary": "FastAPI",
                        "services": [],
                        "dockerfiles": {},
                        "risks": [],
                        "confidence": 0.9,
                        "_cache_package_path": ".",
                    },
                }
            ]
        ),
    )

    response = _client().post(
        "/analyze",
        json={"repo_url": "https://github.com/acme/repo", "commit_sha": "sha-cached"},
    )

    assert response.status_code == 401


def test_analyze_rejects_unauthenticated_when_cache_missing(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)
    monkeypatch.setattr(db_module, "supabase", FakeSupabase(cache_rows=[]))
    monkeypatch.setattr(app_module.graph, "invoke", lambda *_args, **_kwargs: {"commit_sha": "x"})

    response = _client().post(
        "/analyze",
        json={"repo_url": "https://github.com/acme/repo", "commit_sha": "missing"},
        # no auth header on purpose
    )

    assert response.status_code == 401


def test_feedback_stream_success_emits_progress_and_complete(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)
    fake_supabase = FakeSupabase(
        cache_rows=[
            {
                "id": "1",
                "repo_url": "https://github.com/acme/repo",
                "commit_sha": "sha-1",
                "package_path": ".",
                "service_name": None,
                "result": {
                    "commit_sha": "sha-1",
                    "stack_summary": "FastAPI",
                    "services": [{"name": "api", "build_context": ".", "port": 8000}],
                    "dockerfiles": {"api": "FROM python:3.11"},
                    "docker_compose": "services:\n  api:\n    build: .\n",
                    "nginx_conf": "events {}\nhttp { server { listen 80; } }\n",
                    "has_existing_dockerfiles": False,
                    "has_existing_compose": False,
                    "risks": ["old-risk"],
                    "confidence": 0.5,
                    "hadolint_results": {"api": "old"},
                    "_cache_package_path": ".",
                },
            }
        ]
    )
    monkeypatch.setattr(db_module, "supabase", fake_supabase)

    async def fake_feedback_astream(_initial_state, config=None):
        callbacks = config.get("callbacks", []) if config else []
        assert len(callbacks) == 1
        yield {"feedback_coordinator": {"change_plan": []}}
        yield {
            "feedback_verifier": {
                "commit_sha": "sha-1",
                "detected_stack": "FastAPI",
                "services": [{"name": "api", "build_context": ".", "port": 8000}],
                "dockerfiles": {"Dockerfile": "FROM python:3.12"},
                "docker_compose": "services:\n  api:\n    build: .\n",
                "nginx_conf": "events {}\nhttp { server { listen 80; } }\n",
                "has_existing_dockerfiles": False,
                "has_existing_compose": False,
                "risks": ["new-risk"],
                "confidence": 0.91,
                "hadolint_results": {"api": ""},
            }
        }

    monkeypatch.setattr("graph.feedback.feedback_graph.astream", fake_feedback_astream)

    response = _client().post(
        "/feedback/stream",
        json={
            "repo_url": "https://github.com/acme/repo",
            "commit_sha": "sha-1",
            "feedback": "fix api healthcheck",
        },
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert events[0][0] == "progress"
    assert events[-1][0] == "complete"
    assert events[-1][1]["commit_sha"] == "sha-1"
    assert events[-1][1]["confidence"] == 0.91
    assert events[-1][1]["token_usage"]["total_tokens"] == 18
    assert len(fake_supabase.upserted_payloads) == 1


def test_feedback_stream_emits_error_when_cache_missing(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)
    monkeypatch.setattr(db_module, "supabase", FakeSupabase(cache_rows=[]))

    response = _client().post(
        "/feedback/stream",
        json={
            "repo_url": "https://github.com/acme/repo",
            "commit_sha": "missing",
            "feedback": "fix api",
        },
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert len(events) == 1
    assert events[0][0] == "error"
    assert "No cached analysis found" in events[0][1]["detail"]


class _FakeFetchTool:
    def __init__(self, payload):
        self.payload = payload

    def invoke(self, _args):
        return dict(self.payload)


def _install_fake_scanner(monkeypatch, scan_payload):
    import graph.nodes.scanner as scanner_module

    monkeypatch.setattr(scanner_module, "fetch_repo_structure", _FakeFetchTool(scan_payload))


def test_analyze_rejects_broad_root_scope_with_suggestions(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)
    monkeypatch.setattr(db_module, "supabase", None)
    monkeypatch.setenv("SD_SCOPE_GUARD_ENABLED", "true")
    monkeypatch.setenv("SD_SCOPE_GUARD_TREE_THRESHOLD", "3000")
    monkeypatch.setenv("SD_SCOPE_GUARD_PACKAGE_THRESHOLD", "20")

    _install_fake_scanner(
        monkeypatch,
        {
            "repo_full_name": "acme/repo",
            "default_branch": "main",
            "commit_sha": "sha-1",
            "language": "TypeScript",
            "key_files": {},
            "dirs": [],
            "tree_entry_count": 5200,
            "candidate_package_paths": [f"apps/app-{i}" for i in range(1, 26)],
            "candidate_service_hints": ["api", "web", "worker"],
        },
    )

    def fake_invoke(initial_state, config=None):
        import graph.nodes.scanner as scanner_module

        return scanner_module.scanner_node(dict(initial_state))

    monkeypatch.setattr(app_module.graph, "invoke", fake_invoke)

    response = _client().post(
        "/analyze",
        json={"repo_url": "https://github.com/acme/repo", "package_path": "."},
        headers=_auth_headers(),
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "scope_required"
    assert detail["tree_entry_count"] == 5200
    assert len(detail["suggested_package_paths"]) == 10
    assert detail["suggested_service_names"] == ["api", "web", "worker"]


def test_analyze_stream_emits_scope_required_error_event(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)
    monkeypatch.setattr(db_module, "supabase", None)
    monkeypatch.setenv("SD_SCOPE_GUARD_ENABLED", "true")
    monkeypatch.setenv("SD_SCOPE_GUARD_TREE_THRESHOLD", "3000")
    monkeypatch.setenv("SD_SCOPE_GUARD_PACKAGE_THRESHOLD", "20")

    _install_fake_scanner(
        monkeypatch,
        {
            "repo_full_name": "acme/repo",
            "default_branch": "main",
            "commit_sha": "sha-1",
            "language": "TypeScript",
            "key_files": {},
            "dirs": [],
            "tree_entry_count": 4100,
            "candidate_package_paths": [f"services/svc-{i}" for i in range(1, 30)],
            "candidate_service_hints": ["api", "frontend"],
        },
    )

    async def fake_astream(initial_state, config=None):
        import graph.nodes.scanner as scanner_module

        state = scanner_module.scanner_node(dict(initial_state))
        if state.get("error"):
            yield {"scanner": {"error": state["error"]}}
            return
        yield {"scanner": state}

    monkeypatch.setattr(app_module.graph, "astream", fake_astream)

    response = _client().post(
        "/analyze/stream",
        json={"repo_url": "https://github.com/acme/repo", "package_path": "."},
        headers=_auth_headers(),
    )

    events = _parse_sse(response.text)
    assert events[0][0] == "progress"
    assert events[1][0] == "error"
    assert events[1][1]["detail"]["code"] == "scope_required"
    assert len(events[1][1]["detail"]["suggested_package_paths"]) == 10


def test_scoped_package_path_bypasses_scope_guard(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)
    monkeypatch.setattr(db_module, "supabase", None)
    monkeypatch.setenv("SD_SCOPE_GUARD_ENABLED", "true")

    _install_fake_scanner(
        monkeypatch,
        {
            "repo_full_name": "acme/repo",
            "default_branch": "main",
            "commit_sha": "sha-2",
            "language": "TypeScript",
            "key_files": {},
            "dirs": [],
            "tree_entry_count": 7000,
            "candidate_package_paths": [f"apps/app-{i}" for i in range(1, 40)],
            "candidate_service_hints": ["dashboard"],
        },
    )

    def fake_invoke(initial_state, config=None):
        import graph.nodes.scanner as scanner_module

        state = scanner_module.scanner_node(dict(initial_state))
        if state.get("error"):
            return state
        state.update(
            {
                "detected_stack": "Next.js",
                "stack_tokens": ["next", "react"],
                "services": [{"name": "dashboard", "build_context": "apps/dashboard", "port": 3000}],
                "dockerfiles": {"apps/dashboard/Dockerfile": "FROM node:20"},
                "risks": [],
                "confidence": 0.9,
            }
        )
        return state

    monkeypatch.setattr(app_module.graph, "invoke", fake_invoke)

    response = _client().post(
        "/analyze",
        json={"repo_url": "https://github.com/acme/repo", "package_path": "apps/dashboard"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json()["commit_sha"] == "sha-2"


def test_service_name_bypasses_scope_guard_when_unique(monkeypatch):
    _set_auth(monkeypatch)
    _set_common_mocks(monkeypatch)
    monkeypatch.setattr(db_module, "supabase", None)
    monkeypatch.setenv("SD_SCOPE_GUARD_ENABLED", "true")

    _install_fake_scanner(
        monkeypatch,
        {
            "repo_full_name": "acme/repo",
            "default_branch": "main",
            "commit_sha": "sha-3",
            "language": "TypeScript",
            "key_files": {},
            "dirs": [],
            "tree_entry_count": 9000,
            "candidate_package_paths": [f"apps/app-{i}" for i in range(1, 45)],
            "candidate_service_hints": ["api", "worker"],
        },
    )

    def fake_invoke(initial_state, config=None):
        import graph.nodes.scanner as scanner_module

        state = scanner_module.scanner_node(dict(initial_state))
        if state.get("error"):
            return state
        state.update(
            {
                "detected_stack": "FastAPI",
                "stack_tokens": ["python", "fastapi"],
                "services": [{"name": "api", "build_context": "services/api", "port": 8000}],
                "dockerfiles": {"services/api/Dockerfile": "FROM python:3.11"},
                "risks": [],
                "confidence": 0.87,
            }
        )
        return state

    monkeypatch.setattr(app_module.graph, "invoke", fake_invoke)

    response = _client().post(
        "/analyze",
        json={"repo_url": "https://github.com/acme/repo", "package_path": ".", "service_name": "api"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json()["services"][0]["name"] == "api"


def test_scope_guard_threshold_env_overrides(monkeypatch):
    monkeypatch.setattr(db_module, "supabase", None)
    _install_fake_scanner(
        monkeypatch,
        {
            "repo_full_name": "acme/repo",
            "default_branch": "main",
            "commit_sha": "sha-4",
            "language": "TypeScript",
            "key_files": {},
            "dirs": [],
            "tree_entry_count": 5000,
            "candidate_package_paths": [f"apps/app-{i}" for i in range(1, 30)],
            "candidate_service_hints": ["api"],
        },
    )

    import graph.nodes.scanner as scanner_module

    monkeypatch.setenv("SD_SCOPE_GUARD_ENABLED", "true")
    monkeypatch.setenv("SD_SCOPE_GUARD_TREE_THRESHOLD", "10000")
    monkeypatch.setenv("SD_SCOPE_GUARD_PACKAGE_THRESHOLD", "100")
    state = scanner_module.scanner_node({"repo_url": "https://github.com/acme/repo", "package_path": ".", "service_name": None})
    assert "error" not in state

    monkeypatch.setenv("SD_SCOPE_GUARD_TREE_THRESHOLD", "100")
    monkeypatch.setenv("SD_SCOPE_GUARD_PACKAGE_THRESHOLD", "2")
    state = scanner_module.scanner_node({"repo_url": "https://github.com/acme/repo", "package_path": ".", "service_name": None})
    assert state["error"]["code"] == "scope_required"
