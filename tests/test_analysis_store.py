import pytest

import services.analysis_store as analysis_store


class FakeExecuteResponse:
    def __init__(self, data):
        self.data = data


class FakeTableQuery:
    def __init__(self, rows):
        self.rows = rows
        self.filters = {}
        self.expect_single = False

    def select(self, _columns):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def is_(self, key, value):
        self.filters[key] = value
        return self

    def single(self):
        self.expect_single = True
        return self

    def execute(self):
        rows = [row for row in self.rows if self._matches(row)]
        if self.expect_single:
            if len(rows) != 1:
                raise RuntimeError("single row not found")
            return FakeExecuteResponse(rows[0])
        return FakeExecuteResponse(rows)

    def _matches(self, row):
        for key, value in self.filters.items():
            if row.get(key) != value:
                return False
        return True


class FakeSupabase:
    def __init__(self, cache_rows=None, response_rows=None):
        self.cache_rows = cache_rows or []
        self.response_rows = response_rows or []

    def table(self, table_name):
        if table_name == "analysis_cache":
            return FakeTableQuery(self.cache_rows)
        if table_name == "analysis_responses":
            return FakeTableQuery(self.response_rows)
        raise AssertionError(f"Unexpected table: {table_name}")


def test_get_analysis_response_by_id_returns_curated_payload():
    fake_supabase = FakeSupabase(
        response_rows=[
            {
                "id": "resp-1",
                "endpoint": "/feedback",
                "repo_url": "https://github.com/acme/repo",
                "commit_sha": "abc123",
                "package_path": "./services/api",
                "service_name": " api ",
                "from_cache": True,
                "passed": True,
                "created_at": "2026-05-19T22:00:00Z",
                "payload": {
                    "response_id": "resp-1",
                    "_cache_package_path": "services/api",
                    "stack_summary": "FastAPI",
                },
            }
        ]
    )

    result = analysis_store.get_analysis_response_by_id("resp-1", supabase_client=fake_supabase)

    assert result == {
        "id": "resp-1",
        "endpoint": "/feedback",
        "repo_url": "https://github.com/acme/repo",
        "commit_sha": "abc123",
        "package_path": "services/api",
        "service_name": "api",
        "from_cache": True,
        "passed": True,
        "created_at": "2026-05-19T22:00:00Z",
        "payload": {
            "response_id": "resp-1",
            "stack_summary": "FastAPI",
        },
    }


def test_get_analysis_cache_entry_strips_internal_metadata():
    fake_supabase = FakeSupabase(
        cache_rows=[
            {
                "response_id": "resp-2",
                "repo_url": "https://github.com/acme/repo",
                "commit_sha": "abc123",
                "package_path": "services/api",
                "service_name": None,
                "created_at": "2026-05-19T22:10:00Z",
                "result": {
                    "_cache_package_path": "services/api",
                    "stack_summary": "FastAPI",
                },
            }
        ]
    )

    result = analysis_store.get_analysis_cache_entry(
        repo_url="https://github.com/acme/repo",
        commit_sha="abc123",
        package_path="./services/api",
        supabase_client=fake_supabase,
    )

    assert result == {
        "response_id": "resp-2",
        "repo_url": "https://github.com/acme/repo",
        "commit_sha": "abc123",
        "package_path": "services/api",
        "service_name": None,
        "created_at": "2026-05-19T22:10:00Z",
        "result": {
            "stack_summary": "FastAPI",
        },
    }


def test_get_analysis_cache_entry_raises_not_found():
    fake_supabase = FakeSupabase(cache_rows=[])

    with pytest.raises(analysis_store.AnalysisStoreNotFoundError):
        analysis_store.get_analysis_cache_entry(
            repo_url="https://github.com/acme/repo",
            commit_sha="missing",
            supabase_client=fake_supabase,
        )


def test_get_analysis_response_by_id_raises_when_supabase_missing(monkeypatch):
    monkeypatch.setattr("db.supabase", None)

    with pytest.raises(analysis_store.AnalysisStoreNotConfiguredError):
        analysis_store.get_analysis_response_by_id("resp-1")
