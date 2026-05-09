import graph.nodes.build_verify as mod
from graph.nodes.build_verify import build_verify_node


def test_build_verify_skipped_when_disabled(monkeypatch):
    monkeypatch.delenv("SD_RAILPACK_VERIFY_ENABLED", raising=False)
    state = build_verify_node({"repo_url": "https://github.com/example/repo"})
    assert state["build_verification"]["status"] == "skipped"


def test_build_verify_unavailable_without_binary(monkeypatch):
    monkeypatch.setenv("SD_RAILPACK_VERIFY_ENABLED", "true")
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)
    state = build_verify_node({"repo_url": "https://github.com/example/repo"})
    assert state["build_verification"]["status"] == "unavailable"
