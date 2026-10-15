"""The API reads a private repository with a read token from a credential-store file, never
with the token on the command line (ADR-091)."""

import subprocess

import pytest

import app.main as main


def fake_git(calls):
    def run(argv, **kwargs):
        calls.append((argv, kwargs.get("env", {})))
        if "ls-remote" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{'a' * 40}\trefs/heads/main\n", "")
        if "log" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{'a' * 40}\x1ffix\x1fdana\x1f2026-10-15T00:00:00Z\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


def test_reads_are_anonymous_without_a_credentials_file(monkeypatch):
    monkeypatch.delenv("NETCI_GIT_CREDENTIALS_FILE", raising=False)
    calls = []
    monkeypatch.setattr(main.subprocess, "run", fake_git(calls))
    assert main.list_remote_refs("http://git.example/app.git")["branches"] == [{"name": "main", "sha": "a" * 40}]
    assert not any("credential.helper" in " ".join(argv) for argv, _ in calls)


def test_a_credentials_file_is_the_only_helper_and_the_token_never_an_argument(monkeypatch, tmp_path):
    store = tmp_path / "git-credentials"
    store.write_text("http://oauth2:s3cr3t-token@git.example\n")
    store.chmod(0o600)
    monkeypatch.setenv("NETCI_GIT_CREDENTIALS_FILE", str(store))
    calls = []
    monkeypatch.setattr(main.subprocess, "run", fake_git(calls))
    main.list_remote_refs("http://git.example/app.git")
    main.list_recent_commits("http://git.example/app.git", "main")
    dialled = [argv for argv, _ in calls if "ls-remote" in argv or "fetch" in argv]
    assert len(dialled) == 2
    for argv in dialled:
        joined = " ".join(argv)
        # The user's own helpers are reset, then only the store answers.
        assert argv[1:5] == ["-c", "credential.helper=", "-c", f"credential.helper=store --file={store}"]
        assert "s3cr3t-token" not in joined
    for _, env in calls:
        assert "s3cr3t-token" not in " ".join(f"{k}={v}" for k, v in env.items())
        assert env.get("GIT_TERMINAL_PROMPT") == "0"


def test_a_credentials_file_that_cannot_be_read_is_refused_not_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("NETCI_GIT_CREDENTIALS_FILE", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="cannot be read"):
        main.list_remote_refs("http://git.example/app.git")
