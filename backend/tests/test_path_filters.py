"""Path filters on delivery triggers (ADR-051): skip a build only when it is known that
nothing it cares about changed. Not knowing is never a reason to skip."""

from __future__ import annotations

import json
from uuid import UUID

import pytest

import app.main as main_mod
from app.adapters.scm import GitHubScmProvider, GitLabScmProvider
from app.domain.delivery_rules import DeliveryRuleError, ScmEvent, decide, parse_rules
from app.domain.models import Environment

from test_ci_cd_separation import _hook, _module, fresh_platform  # noqa: F401 - the fixture is autouse

BEFORE = "1" * 40
AFTER = "2" * 40


def _github_push(**overrides):
    payload = {"ref": "refs/heads/main", "before": BEFORE, "after": AFTER,
               "repository": {"full_name": "acme/app"}, "sender": {"login": "agent"},
               "commits": [{"added": ["docs/new.md"], "removed": [], "modified": ["README.md"]},
                           {"added": [], "removed": ["docs/old.md"], "modified": []}]}
    payload.update(overrides)
    headers = {"x-github-event": "push", "x-github-delivery": "d-1"}
    return GitHubScmProvider().parse_webhook(headers, json.dumps(payload).encode())


def test_a_github_push_reports_every_file_its_commits_touched():
    assert _github_push().changed_files == ("README.md", "docs/new.md", "docs/old.md")


@pytest.mark.parametrize("overrides", [
    {"created": True},                                   # a new branch lists only some commits
    {"forced": True},                                    # rewritten history
    {"before": "0" * 40},
    {"commits": []},                                     # nothing listed is not "nothing changed"
    {"commits": [{"added": [], "removed": [], "modified": []}]},
    {"commits": [{"added": "docs/x.md"}]},               # malformed
    {"commits": [{"added": [], "removed": [], "modified": ["a"]}] * 2048},  # at the cap: maybe cut
])
def test_a_github_push_that_cannot_say_everything_it_changed_reports_nothing(overrides):
    assert _github_push(**overrides).changed_files is None


def test_a_gitlab_push_is_complete_only_when_it_lists_every_commit():
    def parse(total):
        payload = {"ref": "refs/heads/main", "before": BEFORE, "after": AFTER, "checkout_sha": AFTER,
                   "total_commits_count": total, "project": {"path_with_namespace": "acme/app"},
                   "commits": [{"added": ["a.py"], "removed": [], "modified": []}]}
        return GitLabScmProvider().parse_webhook({"x-gitlab-event": "Push Hook"}, json.dumps(payload).encode())

    assert parse(1).changed_files == ("a.py",)
    assert parse(25).changed_files is None


def _rules(*triggers):
    return parse_rules({"triggers": list(triggers)}, default_environment=Environment.DEV,
                       configured=[Environment.DEV, Environment.STAGING])


def _push(files, branch="main"):
    return ScmEvent(kind="push", branch=branch, changed_files=files)


def test_paths_ignore_skips_a_push_that_changed_only_ignored_files():
    rules = _rules({"on": "push", "branches": ["main"], "pathsIgnore": ["docs/**", "*.md"], "deployTo": "dev"})
    assert decide(rules, _push(("docs/a.md", "README.md"))).run is False
    assert decide(rules, _push(("docs/a.md", "src/app.py"))).run is True


def test_paths_runs_only_when_a_listed_path_changed():
    rules = _rules({"on": "push", "branches": ["main"], "paths": ["src/**"]})
    assert decide(rules, _push(("docs/a.md",))).run is False
    assert decide(rules, _push(("src/deep/x.py",))).run is True


def test_a_filtered_out_rule_falls_through_to_the_next_like_any_rule_that_does_not_match():
    rules = _rules(
        {"on": "push", "branches": ["main"], "pathsIgnore": ["docs/**"], "deployTo": "dev"},
        {"on": "push", "branches": ["main"]},
    )
    decision = decide(rules, _push(("docs/a.md",)))
    assert decision.run and decision.rule_index == 1 and decision.deploy_to is None


def test_unknown_changed_files_never_skip_a_build():
    rules = _rules({"on": "push", "branches": ["main"], "pathsIgnore": ["**"]})
    decision = decide(rules, _push(None))
    assert decision.run and "path filter not applied" in decision.reason


@pytest.mark.parametrize("trigger, message", [
    ({"on": "push", "branches": ["main"], "paths": ["src/**"], "pathsIgnore": ["docs/**"]}, "not both"),
    ({"on": "tag", "tags": ["v*"], "paths": ["src/**"]}, "cannot filter on paths"),
    ({"on": "push", "branches": ["main"], "paths": []}, "non-empty list"),
])
def test_path_filters_are_validated(trigger, message):
    with pytest.raises(DeliveryRuleError, match=message):
        _rules(trigger)


def test_a_docs_only_push_starts_nothing_through_the_webhook():
    module, repo = _module("docs-api", delivery={"triggers": [
        {"on": "push", "branches": ["main"], "pathsIgnore": ["docs/**"], "deployTo": "dev"},
    ]})

    def push(files):
        return _hook("push", {"repository": {"full_name": repo}, "ref": "refs/heads/main", "before": BEFORE,
                              "after": AFTER, "sender": {"login": "agent"},
                              "commits": [{"added": files, "removed": [], "modified": []}]})

    skipped = push(["docs/guide.md"])
    assert skipped.status_code == 200 and skipped.json()["status"] == "ignored", skipped.text
    built = push(["docs/guide.md", "src/main.py"])
    assert built.status_code == 201, built.text
    run = main_mod.platform.get_pipeline(UUID(built.json()["pipelineRunId"]))
    assert run.trigger["changedFiles"] == 2
