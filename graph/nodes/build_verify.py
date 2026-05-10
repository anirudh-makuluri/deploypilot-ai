from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, value)


def is_railpack_verify_enabled() -> bool:
    return _env_bool("SD_RAILPACK_VERIFY_ENABLED", False)


def _build_verification_result(
    status: str,
    message: str,
    log_excerpt: str = "",
    duration_seconds: float = 0.0,
) -> Dict[str, Any]:
    return {
        "backend": "railpack",
        "status": status,
        "message": message,
        "log_excerpt": log_excerpt,
        "duration_seconds": round(float(duration_seconds), 2),
    }


def build_verify_node(state: Dict[str, Any]) -> Dict[str, Any]:
    if not is_railpack_verify_enabled():
        state["build_verification"] = _build_verification_result(
            status="skipped",
            message="Railpack verification disabled. Set SD_RAILPACK_VERIFY_ENABLED=true to enable.",
        )
        return state

    repo_url = (state.get("repo_url") or "").strip()
    github_token = state.get("github_token")
    commit_sha = (state.get("commit_sha") or "").strip()
    timeout_seconds = _env_int("SD_RAILPACK_VERIFY_TIMEOUT_SECONDS", 300)
    max_log_chars = _env_int("SD_RAILPACK_VERIFY_MAX_LOG_CHARS", 8000, minimum=500)
    started = time.monotonic()

    if not repo_url:
        state["build_verification"] = _build_verification_result(
            status="error",
            message="Missing repo_url; cannot run Railpack verification.",
        )
        return state

    if shutil.which("railpack") is None:
        state["build_verification"] = _build_verification_result(
            status="unavailable",
            message="Railpack CLI is not installed on this host.",
        )
        return state

    with tempfile.TemporaryDirectory(prefix="sd-railpack-") as tmp_dir:
        repo_dir = os.path.join(tmp_dir, "repo")
        clone_url = repo_url
        if github_token and repo_url.startswith("https://github.com/"):
            clone_url = repo_url.replace("https://github.com/", f"https://{github_token}@github.com/")

        clone_cmd = ["git", "clone", "--depth", "1", clone_url, repo_dir]

        clone_result = subprocess.run(clone_cmd, text=True, capture_output=True, check=False)
        if clone_result.returncode != 0:
            logs = (clone_result.stdout or "") + "\n" + (clone_result.stderr or "")
            state["build_verification"] = _build_verification_result(
                status="error",
                message="Failed to clone repository for Railpack verification.",
                log_excerpt=logs[-max_log_chars:],
                duration_seconds=time.monotonic() - started,
            )
            return state

        if commit_sha and commit_sha != "unknown":
            checkout_result = subprocess.run(
                ["git", "-C", repo_dir, "checkout", commit_sha],
                text=True,
                capture_output=True,
                check=False,
            )
            if checkout_result.returncode != 0:
                logs = (checkout_result.stdout or "") + "\n" + (checkout_result.stderr or "")
                state["build_verification"] = _build_verification_result(
                    status="error",
                    message=f"Cloned repo but failed to checkout commit {commit_sha}.",
                    log_excerpt=logs[-max_log_chars:],
                    duration_seconds=time.monotonic() - started,
                )
                return state

        railpack_cmd = ["railpack", "build", repo_dir]
        try:
            railpack_result = subprocess.run(
                railpack_cmd,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            combined = (str(exc.stdout) or "") + "\n" + (str(exc.stderr) or "")
            state["build_verification"] = _build_verification_result(
                status="failed",
                message=f"Railpack build timed out after {timeout_seconds}s.",
                log_excerpt=combined[-max_log_chars:],
                duration_seconds=time.monotonic() - started,
            )
            return state
        logs = (str(railpack_result.stdout) or "") + "\n" + (str(railpack_result.stderr) or "")
        state["build_verification"] = _build_verification_result(
            status="passed" if railpack_result.returncode == 0 else "failed",
            message="Railpack build succeeded." if railpack_result.returncode == 0 else "Railpack build failed.",
            log_excerpt=logs[-max_log_chars:],
            duration_seconds=time.monotonic() - started,
        )
        return state
