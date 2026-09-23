"""The run dialog can offer commits, not just a branch tip.

It used to show only each branch's head SHA, so building anything else meant pasting hex
from somewhere. These run real git against a real repository built for the test; the
only substitution is allowing file:// here, which production refuses.
"""

from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

import app.main as main


@pytest.fixture()
def repository(tmp_path, monkeypatch):
    work = tmp_path / "work"
    bare = tmp_path / "app.git"
    run = lambda *args, cwd=None: subprocess.run(args, cwd=cwd, check=True, capture_output=True)
    run("git", "init", "-q", "-b", "main", str(work))
    for number in range(1, 4):
        (work / "file.txt").write_text(f"change {number}\n")
        run("git", "add", "-A", cwd=work)
        run("git", "-c", "user.name=Dana", "-c", "user.email=dana@example.test",
            "commit", "-q", "-m", f"change number {number}", cwd=work)
    run("git", "clone", "-q", "--bare", str(work), str(bare))
    monkeypatch.setattr(main, "GIT_URL_SCHEMES", frozenset({"http", "https", "ssh", "file"}))
    return f"file://{bare}"


def test_recent_commits_come_newest_first_with_subject_author_and_time(repository):
    commits = main.list_recent_commits(repository, "main", limit=2)
    assert [c["subject"] for c in commits] == ["change number 3", "change number 2"]
    assert all(c["author"] == "Dana" and len(c["sha"]) == 40 and c["committedAt"] for c in commits)


@pytest.mark.parametrize("ref", ["--upload-pack=touch /tmp/pwned", "-c", "", "../main"])
def test_a_ref_that_could_be_read_as_an_option_is_refused(repository, ref):
    with pytest.raises(ValueError):
        main.list_recent_commits(repository, ref)


def test_production_never_dials_a_file_url():
    with pytest.raises(ValueError):
        main.list_recent_commits("file:///etc", "main")


def _as_module(monkeypatch, url):
    monkeypatch.setattr(main.portal, "module", lambda module_id: {"id": module_id, "repositoryUrl": url})
    monkeypatch.setattr(main, "_require_module_access", lambda module_id, principal: None)


def test_the_endpoint_refuses_a_malformed_ref_instead_of_answering_empty(monkeypatch):
    _as_module(monkeypatch, "https://git.example/app.git")
    response = TestClient(main.app).get("/modules/app/git-commits", params={"ref": "--help"})
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_GIT_REF"


def test_an_unreadable_repository_is_an_error_not_an_invented_history(monkeypatch):
    _as_module(monkeypatch, "https://127.0.0.1:9/nothing-listens-here.git")
    response = TestClient(main.app).get("/modules/app/git-commits", params={"ref": "main"})
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["error"]
