from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List


def _normalize_ctx(path: str) -> str:
    normalized = (path or ".").replace("\\", "/").strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    while "/./" in normalized:
        normalized = normalized.replace("/./", "/")
    if normalized.endswith("/."):
        normalized = normalized[:-2]
    return normalized or "."


def _extract_copy_sources(line: str) -> List[str]:
    # Shell form: COPY src dst or COPY src1 src2 dst
    # JSON form: COPY ["src", "dst"]
    stripped = line.strip()
    if not stripped.lower().startswith("copy "):
        return []

    payload = stripped[5:].strip()
    if payload.startswith("--from="):
        return []
    if " --from=" in payload:
        return []

    if payload.startswith("["):
        try:
            arr = json.loads(payload)
            if isinstance(arr, list) and len(arr) >= 2:
                return [str(x) for x in arr[:-1]]
        except Exception:
            return []
        return []

    # remove flags like --chown
    tokens = []
    for token in re.split(r"\s+", payload):
        if token.startswith("--"):
            continue
        tokens.append(token)
    if len(tokens) < 2:
        return []
    return tokens[:-1]


def _path_matches_known(path: str, known_files: set[str], known_dirs: set[str]) -> bool:
    normalized = _normalize_ctx(path)
    if normalized in {".", ""}:
        return True
    if normalized in known_files or normalized in known_dirs:
        return True
    if "*" in normalized:
        prefix = normalized.split("*", 1)[0].rstrip("/")
        if not prefix:
            return True
        return any(k.startswith(prefix) for k in known_files | known_dirs)
    return any(k.startswith(normalized + "/") for k in known_files | known_dirs)


def preflight_node(state: Dict[str, Any]) -> Dict[str, Any]:
    services = state.get("services", [])
    dockerfiles = state.get("dockerfiles", {})
    scan = state.get("repo_scan", {}) if isinstance(state.get("repo_scan", {}), dict) else {}
    key_files = scan.get("key_files", {}) if isinstance(scan.get("key_files", {}), dict) else {}
    dirs = scan.get("dirs", []) if isinstance(scan.get("dirs", []), list) else []

    known_files = {_normalize_ctx(str(p)) for p in key_files.keys()}
    known_dirs = {_normalize_ctx(str(d)) for d in dirs}
    issues: List[str] = []

    for svc in services:
        if not isinstance(svc, dict):
            continue
        name = str(svc.get("name", "") or "service")
        build_ctx = _normalize_ctx(str(svc.get("build_context", ".") or "."))
        dockerfile_path = str(svc.get("dockerfile_path", "") or "").strip()
        docker_key = dockerfile_path or ("Dockerfile" if build_ctx == "." else f"{build_ctx}/Dockerfile")
        content = dockerfiles.get(docker_key)
        if not isinstance(content, str):
            continue

        for line in content.splitlines():
            sources = _extract_copy_sources(line)
            for src in sources:
                src_norm = _normalize_ctx(src)
                if src_norm.startswith(".."):
                    issues.append(f"{name}: COPY source '{src}' escapes build context '{build_ctx}'")
                    continue
                if build_ctx != ".":
                    candidate = _normalize_ctx(f"{build_ctx}/{src_norm}")
                else:
                    candidate = src_norm
                if not _path_matches_known(candidate, known_files, known_dirs):
                    issues.append(
                        f"{name}: COPY source '{src}' not found under build context '{build_ctx}' (checked as '{candidate}')"
                    )

    state["preflight_issues"] = issues
    strict_raw = os.getenv("SD_PREFLIGHT_STRICT", "true")
    strict_enabled = strict_raw.strip().lower() in {"1", "true", "yes", "on"}
    if strict_enabled and issues:
        state["error"] = {
            "code": "preflight_failed",
            "message": "Static preflight checks failed for generated Dockerfiles.",
            "issues": issues,
        }
    return state
