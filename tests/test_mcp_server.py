import asyncio
import json

from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextResourceContents

import mcp_server


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


def _read_text_resource(result) -> dict:
    content = result.contents[0]
    assert isinstance(content, TextResourceContents)
    return json.loads(content.text)


def test_build_analysis_cache_uri_supports_default_and_scoped_reads():
    assert (
        mcp_server.build_analysis_cache_uri("https://github.com/acme/repo", "abc123")
        == "analysis-cache://aHR0cHM6Ly9naXRodWIuY29tL2FjbWUvcmVwbw/abc123"
    )
    assert (
        mcp_server.build_analysis_cache_uri(
            "https://github.com/acme/repo",
            "abc123",
            package_path="services/api",
            service_name="api",
        )
        == "analysis-cache://aHR0cHM6Ly9naXRodWIuY29tL2FjbWUvcmVwbw/abc123/c2VydmljZXMvYXBp/YXBp"
    )


def test_mcp_server_reads_analysis_resources(monkeypatch):
    fake_supabase = FakeSupabase(
        response_rows=[
            {
                "id": "resp-1",
                "endpoint": "/analyze",
                "repo_url": "https://github.com/acme/repo",
                "commit_sha": "abc123",
                "package_path": ".",
                "service_name": None,
                "from_cache": False,
                "passed": True,
                "created_at": "2026-05-19T22:00:00Z",
                "payload": {
                    "response_id": "resp-1",
                    "_cache_package_path": ".",
                    "stack_summary": "FastAPI",
                },
            }
        ],
        cache_rows=[
            {
                "response_id": "resp-1",
                "repo_url": "https://github.com/acme/repo",
                "commit_sha": "abc123",
                "package_path": ".",
                "service_name": None,
                "created_at": "2026-05-19T22:05:00Z",
                "result": {
                    "_cache_package_path": ".",
                    "stack_summary": "FastAPI",
                },
            }
        ],
    )
    monkeypatch.setattr("db.supabase", fake_supabase)

    async def _run():
        async with create_connected_server_and_client_session(mcp_server.mcp) as session:
            await session.initialize()

            response_resource = await session.read_resource(mcp_server.build_analysis_response_uri("resp-1"))
            cache_resource = await session.read_resource(
                mcp_server.build_analysis_cache_uri("https://github.com/acme/repo", "abc123")
            )

            response_payload = _read_text_resource(response_resource)
            cache_payload = _read_text_resource(cache_resource)

            assert response_payload["id"] == "resp-1"
            assert response_payload["payload"] == {
                "response_id": "resp-1",
                "stack_summary": "FastAPI",
            }
            assert cache_payload["result"] == {"stack_summary": "FastAPI"}

            templates = await session.list_resource_templates()
            uris = {item.uriTemplate for item in templates.resourceTemplates}
            assert "analysis-response://{response_id}" in uris
            assert "analysis-cache://{repo_url_b64}/{commit_sha}" in uris

    asyncio.run(_run())
