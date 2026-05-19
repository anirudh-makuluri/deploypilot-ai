from __future__ import annotations

import base64
import os
from typing import Literal

from mcp.server.fastmcp import FastMCP

from services.analysis_store import (
    AnalysisStoreError,
    get_analysis_cache_entry,
    get_analysis_response_by_id,
    normalize_package_path,
    normalize_service_name,
)

DEFAULT_TRANSPORT = "stdio"
SUPPORTED_TRANSPORTS = {"stdio", "sse", "streamable-http"}


def _b64_encode(value: str) -> str:
    raw = value.encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64_decode(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")


def build_analysis_response_uri(response_id: str) -> str:
    return f"analysis-response://{response_id}"


def build_analysis_cache_uri(
    repo_url: str,
    commit_sha: str,
    package_path: str = ".",
    service_name: str | None = None,
) -> str:
    repo_url_b64 = _b64_encode(repo_url)
    package_path_b64 = _b64_encode(normalize_package_path(package_path))

    if normalize_service_name(service_name) is None:
        if normalize_package_path(package_path) == ".":
            return f"analysis-cache://{repo_url_b64}/{commit_sha}"
        return f"analysis-cache://{repo_url_b64}/{commit_sha}/{package_path_b64}"

    service_name_b64 = _b64_encode(normalize_service_name(service_name) or "")
    return f"analysis-cache://{repo_url_b64}/{commit_sha}/{package_path_b64}/{service_name_b64}"


def _build_server() -> FastMCP:
    instructions = (
        "Read-only MCP server for SD-Artifacts. "
        "Use analysis-response resources for stored historical responses by response_id. "
        "Use analysis-cache resources for exact cache-key lookups. "
        "analysis-cache URI parameters are base64url-encoded for repo_url, package_path, and service_name segments."
    )
    return FastMCP(
        name="SD-Artifacts MCP",
        instructions=instructions,
        host=os.getenv("SD_MCP_HOST", "127.0.0.1"),
        port=int(os.getenv("SD_MCP_PORT", "8001")),
        mount_path=os.getenv("SD_MCP_MOUNT_PATH", "/"),
        streamable_http_path=os.getenv("SD_MCP_STREAMABLE_HTTP_PATH", "/mcp"),
        json_response=True,
    )


mcp = _build_server()


def _read_analysis_response(response_id: str) -> dict:
    try:
        return get_analysis_response_by_id(response_id)
    except AnalysisStoreError as exc:
        raise ValueError(str(exc)) from exc


def _read_analysis_cache(repo_url_b64: str, commit_sha: str, package_path: str, service_name: str | None) -> dict:
    repo_url = _b64_decode(repo_url_b64)
    try:
        return get_analysis_cache_entry(
            repo_url=repo_url,
            commit_sha=commit_sha,
            package_path=package_path,
            service_name=service_name,
        )
    except AnalysisStoreError as exc:
        raise ValueError(str(exc)) from exc


@mcp.resource(
    "analysis-response://{response_id}",
    name="analysis_response",
    title="Analysis Response",
    description="Read one stored analysis response log row by response_id.",
    mime_type="application/json",
)
def analysis_response_resource(response_id: str) -> dict:
    return _read_analysis_response(response_id)


@mcp.resource(
    "analysis-cache://{repo_url_b64}/{commit_sha}",
    name="analysis_cache_root",
    title="Analysis Cache",
    description="Read a cached full-repo analysis snapshot by encoded repo_url and commit_sha.",
    mime_type="application/json",
)
def analysis_cache_root_resource(repo_url_b64: str, commit_sha: str) -> dict:
    return _read_analysis_cache(repo_url_b64, commit_sha, package_path=".", service_name=None)


@mcp.resource(
    "analysis-cache://{repo_url_b64}/{commit_sha}/{package_path_b64}",
    name="analysis_cache_package",
    title="Analysis Cache",
    description="Read a cached package-scoped analysis snapshot by encoded repo_url, commit_sha, and package_path.",
    mime_type="application/json",
)
def analysis_cache_package_resource(repo_url_b64: str, commit_sha: str, package_path_b64: str) -> dict:
    return _read_analysis_cache(
        repo_url_b64,
        commit_sha,
        package_path=_b64_decode(package_path_b64),
        service_name=None,
    )


@mcp.resource(
    "analysis-cache://{repo_url_b64}/{commit_sha}/{package_path_b64}/{service_name_b64}",
    name="analysis_cache_service",
    title="Analysis Cache",
    description=(
        "Read a cached service-scoped analysis snapshot by encoded repo_url, commit_sha, "
        "package_path, and service_name."
    ),
    mime_type="application/json",
)
def analysis_cache_service_resource(
    repo_url_b64: str,
    commit_sha: str,
    package_path_b64: str,
    service_name_b64: str,
) -> dict:
    return _read_analysis_cache(
        repo_url_b64,
        commit_sha,
        package_path=_b64_decode(package_path_b64),
        service_name=_b64_decode(service_name_b64),
    )


def _resolve_transport() -> Literal["stdio", "sse", "streamable-http"]:
    transport = os.getenv("SD_MCP_TRANSPORT", DEFAULT_TRANSPORT).strip().lower()
    if transport not in SUPPORTED_TRANSPORTS:
        valid = ", ".join(sorted(SUPPORTED_TRANSPORTS))
        raise SystemExit(f"Unsupported SD_MCP_TRANSPORT '{transport}'. Expected one of: {valid}")
    return transport  # type: ignore[return-value]


if __name__ == "__main__":
    mcp.run(transport=_resolve_transport())
