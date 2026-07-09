"""The deployed-revision resolver used for the footer build stamp."""
from __future__ import annotations


def test_git_commit_prefers_env_then_stamp_file(monkeypatch, tmp_path):
    from core import version

    # 1. GIT_COMMIT env var wins and is truncated to a short hash.
    version.git_commit.cache_clear()
    monkeypatch.setenv("GIT_COMMIT", "abcdef1234567890fedcba")
    assert version.git_commit() == "abcdef123456"

    # 2. With no env var, the .git-commit stamp file is used (prod source of truth).
    version.git_commit.cache_clear()
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    stamp = tmp_path / ".git-commit"
    stamp.write_text("deadbeefcafe0000\n", encoding="utf-8")
    monkeypatch.setattr(version, "_STAMP", stamp)
    assert version.git_commit() == "deadbeefcafe"
    version.git_commit.cache_clear()


def test_git_commit_is_blank_when_unresolvable(monkeypatch, tmp_path):
    from core import version

    version.git_commit.cache_clear()
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.setattr(version, "_STAMP", tmp_path / "missing")
    # No env, no stamp, and no git binary in the test container → '' (footer hides).
    monkeypatch.setattr(version, "_BASE_DIR", tmp_path)
    assert version.git_commit() == ""
    version.git_commit.cache_clear()
