"""The rules that decide what an SCM event may cause (ADR-043), as pure functions.

Each refusal below is a way a trigger could reach somewhere it must not: production
without a request, a shared environment with unreviewed code, a digest nobody signed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.domain.delivery_rules import (
    DeliveryRuleError,
    ScmEvent,
    decide,
    default_rules,
    matches,
    parse_rules,
    pull_request_ref,
    soak_evidence,
)
from app.domain.models import (
    Deployment,
    DeploymentStatus,
    Environment,
    Runtime,
)

DEV, STAGING, PROD = Environment.DEV, Environment.STAGING, Environment.PROD
ALL = (DEV, STAGING, PROD)


def parse(raw, *, default=DEV, configured=ALL):
    return parse_rules(raw, default_environment=default, configured=configured)


@pytest.mark.parametrize(("pattern", "value", "expected"), [
    ("main", "main", True),
    ("main", "main2", False),
    ("release/*", "release/1.2", True),
    ("release/*", "release/1.2/hotfix", False),   # `*` stops at `/`, as in GitHub
    ("release/**", "release/1.2/hotfix", True),
    ("**", "feature/a/b", True),
    ("v*", "v1.2.3", True),
    ("v*", "nightly", False),
])
def test_patterns_follow_github_branch_filter_semantics(pattern, value, expected):
    assert matches(pattern, value) is expected


def test_by_default_only_main_deploys_and_only_to_the_first_environment():
    rules = default_rules(DEV, ALL)

    main = decide(rules, ScmEvent("push", "main"))
    feature = decide(rules, ScmEvent("push", "feature/login"))
    pr = decide(rules, ScmEvent("pull_request", "main"))

    assert (main.run, main.deploy_to) == (True, DEV)
    assert (feature.run, feature.deploy_to, feature.publish) == (True, None, True)
    assert (pr.run, pr.deploy_to, pr.publish) == (True, None, True)


def test_a_module_whose_default_environment_is_production_deploys_nothing_automatically():
    decision = decide(default_rules(PROD, ALL), ScmEvent("push", "main"))
    assert decision.run and decision.deploy_to is None


def test_a_default_rule_does_not_deploy_to_an_environment_the_module_has_no_target_for():
    decision = decide(default_rules(DEV, (STAGING, PROD)), ScmEvent("push", "main"))
    assert decision.deploy_to is None


def test_a_version_tag_registers_a_version_and_a_tag_that_is_not_one_does_not():
    rules = parse({"triggers": [{"on": "tag", "tags": ["*"], "registerVersion": True}]})

    assert decide(rules, ScmEvent("tag", "", tag="v1.4.0")).release_tag == "v1.4.0"
    other = decide(rules, ScmEvent("tag", "", tag="nightly"))
    assert other.run and other.release_tag is None
    assert "not a version" in other.reason


def test_fork_pull_requests_are_ignored_unless_the_module_opts_in_and_then_never_published():
    ignored = decide(default_rules(DEV, ALL), ScmEvent("pull_request", "main", from_fork=True))
    assert not ignored.run

    verify = decide(parse({"forkPullRequests": "verify"}), ScmEvent("pull_request", "main", from_fork=True))
    assert verify.run and verify.publish is False and verify.deploy_to is None


def test_the_first_matching_rule_wins():
    rules = parse({"triggers": [
        {"on": "push", "branches": ["release/**"], "deployTo": "staging"},
        {"on": "push", "branches": ["**"], "deployTo": "dev"},
    ]})
    assert decide(rules, ScmEvent("push", "release/2.0")).deploy_to == STAGING
    assert decide(rules, ScmEvent("push", "feature/x")).deploy_to == DEV


def test_an_event_no_rule_matches_starts_nothing():
    rules = parse({"triggers": [{"on": "push", "branches": ["main"]}]})
    decision = decide(rules, ScmEvent("pull_request", "main"))
    assert not decision.run and "no rule" in decision.reason


@pytest.mark.parametrize(("raw", "message"), [
    ({"triggers": [{"on": "push", "branches": ["main"], "deployTo": "prod"}]}, "production request"),
    ({"triggers": [{"on": "pull_request", "branches": ["**"], "deployTo": "dev"}]}, "built, not deployed"),
    ({"triggers": [{"on": "push", "branches": ["main"], "registerVersion": True}]}, "only a tag"),
    ({"triggers": [{"on": "merge", "branches": ["main"]}]}, "must be one of"),
    ({"triggers": [{"on": "push", "branches": []}]}, "non-empty"),
    ({"triggers": [{"on": "push", "branches": ["main; rm -rf /"]}]}, "not a branch or tag pattern"),
    ({"triggers": [{"on": "push", "branches": ["../x"]}]}, "not a branch or tag pattern"),
    ({"triggers": [{"on": "push", "branches": ["main"], "deployto": "dev"}]}, "unknown field"),
    ({"triggers": [{"on": "push", "branches": ["main"], "deployTo": "dev"}]}, "no dev target"),
    ({"forkPullRequests": "trust"}, "must be one of"),
    ({"promotion": {"dev": {}}}, "first environment"),
    ({"promotion": {"prod": {"requireHealthyIn": "prod"}}}, "before prod"),
    ({"promotion": {"prod": {"minSoakMinutes": 30}}}, "needs requireHealthyIn"),
    ({"promotion": {"prod": {"requireHealthyIn": "staging", "minSoakMinutes": -1}}}, "0 to 10080"),
    ({"promotion": {"prod": {"requireHealthyIn": "staging", "minSoakMinutes": True}}}, "0 to 10080"),
    ({"rules": []}, "unknown field"),
    ("deploy everything", "must be an object"),
])
def test_malformed_rules_are_refused_not_partly_read(raw, message):
    configured = (STAGING, PROD) if "no dev target" in message else ALL
    with pytest.raises(DeliveryRuleError, match=message):
        parse(raw, configured=configured)


def test_promotion_defaults_ask_staging_for_dev_evidence_and_leave_prod_to_the_module():
    rules = default_rules(DEV, ALL)
    assert rules.promotion[STAGING].require_healthy_in == DEV
    assert PROD not in rules.promotion


@pytest.mark.parametrize(("value", "expected"), [
    ("refs/pull/12/head", "refs/pull/12/head"),
    ("refs/merge-requests/7/head", "refs/merge-requests/7/head"),
    ("refs/heads/main", ""),
    ("refs/pull/12/head:refs/heads/main", ""),
    ("+refs/pull/1/head", ""),
    (None, ""),
])
def test_only_pull_request_refs_reach_a_refspec(value, expected):
    assert pull_request_ref(value) == expected


def _deployment(digest, status=DeploymentStatus.HEALTHY, env=DEV, healthy_at=None):
    return Deployment(application_id=uuid4(), runtime=Runtime.DOCKER, environment=env,
                      artifact_digest=digest, status=status, healthy_at=healthy_at)


NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
DIGEST = "sha256:" + "a" * 64


def test_soak_counts_from_the_healthy_transition_until_now_while_serving():
    current = _deployment(DIGEST, healthy_at=NOW - timedelta(minutes=45))
    evidence = soak_evidence(digest=DIGEST, environment=DEV, deployments=[current], now=NOW)
    assert round(evidence.proven_minutes) == 45 and "serving now" in evidence.detail


def test_soak_stops_when_a_newer_release_replaced_it():
    older = _deployment(DIGEST, healthy_at=NOW - timedelta(minutes=90))
    newer = _deployment("sha256:" + "b" * 64, healthy_at=NOW - timedelta(minutes=60))
    evidence = soak_evidence(digest=DIGEST, environment=DEV, deployments=[older, newer], now=NOW)
    assert round(evidence.proven_minutes) == 30 and "replaced" in evidence.detail


@pytest.mark.parametrize("status", [DeploymentStatus.ROLLED_BACK, DeploymentStatus.FAILED, DeploymentStatus.DEPLOYING])
def test_a_deployment_that_did_not_stay_healthy_proves_nothing(status):
    deployment = _deployment(DIGEST, status=status, healthy_at=NOW - timedelta(hours=5))
    evidence = soak_evidence(digest=DIGEST, environment=DEV, deployments=[deployment], now=NOW)
    assert evidence.deployment_id is None and evidence.proven_minutes == 0


def test_a_deployment_from_before_healthy_at_existed_proves_no_soak():
    evidence = soak_evidence(digest=DIGEST, environment=DEV, deployments=[_deployment(DIGEST)], now=NOW)
    assert evidence.deployment_id is None


def test_soak_in_another_environment_does_not_count():
    deployment = _deployment(DIGEST, env=STAGING, healthy_at=NOW - timedelta(hours=5))
    evidence = soak_evidence(digest=DIGEST, environment=DEV, deployments=[deployment], now=NOW)
    assert evidence.deployment_id is None
