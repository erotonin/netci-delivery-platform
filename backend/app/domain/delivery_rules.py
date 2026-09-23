"""What an SCM event is allowed to cause, and what a promotion must prove first (ADR-043).

CI and CD used to be one act: every run built *and* deployed, and every webhook -- any
branch, any pull request -- deployed to the application's default environment. A push to
`feature/x` replaced what `dev` was serving, and a pull request from a fork ran the Sign
stage with the signing key bound. The model here is the one GitHub Actions and OpenChoreo
share: an event starts a build; whether that build is deployed, and where, is a rule the
module owns; moving a built digest onward is a promotion, which builds nothing.

Everything here is pure: rules in, decision out. The webhook route and the promotion
route apply the decisions; this module never touches the store.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Literal, Mapping

from .models import Deployment, DeploymentStatus, Environment

TriggerEvent = Literal["push", "tag", "pull_request"]
TRIGGER_EVENTS: frozenset[str] = frozenset({"push", "tag", "pull_request"})
FORK_POLICIES: frozenset[str] = frozenset({"ignore", "verify"})

#: A version is registered from a tag only when the tag is one: production requests name
#: versions by this pattern (`ProductionRequestModuleCreate.version`).
SEMVER_TAG = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

#: Environments a rule or a promotion may deploy to without a production request. Prod is
#: absent on purpose: ADR-038/039 put production behind a request and a second person, and
#: a trigger rule that deployed there would be a way round both.
AUTOMATIC_ENVIRONMENTS: tuple[Environment, ...] = (Environment.DEV, Environment.STAGING)
ENVIRONMENT_ORDER: tuple[Environment, ...] = (Environment.DEV, Environment.STAGING, Environment.PROD)


class DeliveryRuleError(ValueError):
    """The module's delivery rules are not valid; the message names the field."""


@dataclass(frozen=True)
class Trigger:
    on: str
    patterns: tuple[str, ...]
    deploy_to: Environment | None = None
    register_version: bool = False

    def as_json(self) -> dict[str, object]:
        key = "tags" if self.on == "tag" else "branches"
        body: dict[str, object] = {"on": self.on, key: list(self.patterns)}
        if self.deploy_to is not None:
            body["deployTo"] = self.deploy_to.value
        if self.register_version:
            body["registerVersion"] = True
        return body


@dataclass(frozen=True)
class PromotionRule:
    require_healthy_in: Environment | None = None
    min_soak_minutes: int = 0

    def as_json(self) -> dict[str, object]:
        return {
            "requireHealthyIn": self.require_healthy_in.value if self.require_healthy_in else None,
            "minSoakMinutes": self.min_soak_minutes,
        }


@dataclass(frozen=True)
class DeliveryRules:
    triggers: tuple[Trigger, ...]
    fork_pull_requests: str = "ignore"
    promotion: Mapping[Environment, PromotionRule] = field(default_factory=dict)
    #: True when the module declared nothing and these are the defaults.
    defaulted: bool = False

    def as_json(self) -> dict[str, object]:
        return {
            "triggers": [t.as_json() for t in self.triggers],
            "forkPullRequests": self.fork_pull_requests,
            "promotion": {env.value: rule.as_json() for env, rule in self.promotion.items()},
            "defaulted": self.defaulted,
        }


@dataclass(frozen=True)
class ScmEvent:
    """An SCM event reduced to what the rules read."""

    kind: TriggerEvent
    #: push: the pushed branch. pull_request: the *target* branch, as GitHub Actions
    #: matches `on.pull_request.branches`. tag: empty.
    branch: str
    tag: str | None = None
    from_fork: bool = False


@dataclass(frozen=True)
class TriggerDecision:
    run: bool
    reason: str
    deploy_to: Environment | None = None
    publish: bool = True
    release_tag: str | None = None
    rule_index: int | None = None


def _glob(pattern: str) -> re.Pattern[str]:
    """GitHub's branch filter semantics: `*` stops at `/`, `**` does not."""

    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(pattern: str, value: str) -> bool:
    return bool(_glob(pattern).match(value))


def default_rules(default_environment: Environment, configured: Iterable[Environment]) -> DeliveryRules:
    """What a module that declared nothing gets.

    A push to `main` deploys to the module's first environment, and nothing else deploys:
    other branches and pull requests are built and tested, tags are built and become
    versions. A module whose default environment is production gets no automatic
    deployment at all -- see AUTOMATIC_ENVIRONMENTS.
    """

    targets = set(configured)
    deploy_to = default_environment if default_environment in AUTOMATIC_ENVIRONMENTS and default_environment in targets else None
    promotion: dict[Environment, PromotionRule] = {}
    if Environment.STAGING in targets:
        promotion[Environment.STAGING] = PromotionRule(
            require_healthy_in=Environment.DEV if Environment.DEV in targets else None
        )
    return DeliveryRules(
        triggers=(
            Trigger("push", ("main",), deploy_to=deploy_to),
            Trigger("push", ("**",)),
            Trigger("pull_request", ("**",)),
            Trigger("tag", ("v*",), register_version=True),
        ),
        promotion=promotion,
        defaulted=True,
    )


def _environment(value: object, where: str) -> Environment:
    try:
        return Environment(str(value))
    except ValueError as exc:
        raise DeliveryRuleError(f"{where}: unknown environment {value!r}") from exc


def _patterns(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(p, str) and p.strip() for p in value):
        raise DeliveryRuleError(f"{where}: must be a non-empty list of patterns")
    for pattern in value:
        # A ref pattern reaches no shell, but it is shown in the portal and matched against
        # refs; the same character class the run API accepts for a branch, plus `*`.
        if not re.fullmatch(r"[0-9A-Za-z._\-/*]+", pattern) or ".." in pattern:
            raise DeliveryRuleError(f"{where}: {pattern!r} is not a branch or tag pattern")
    return tuple(p.strip() for p in value)


def parse_rules(
    raw: object,
    *,
    default_environment: Environment,
    configured: Iterable[Environment],
) -> DeliveryRules:
    """Read `pipelineConfig.delivery`. Absent means the defaults; anything malformed is an
    error rather than a partial reading, because a rule silently ignored is a deployment
    that silently does or does not happen."""

    targets = tuple(configured)
    if raw is None:
        return default_rules(default_environment, targets)
    if not isinstance(raw, dict):
        raise DeliveryRuleError("delivery: must be an object")
    unknown = set(raw) - {"triggers", "forkPullRequests", "promotion"}
    if unknown:
        raise DeliveryRuleError(f"delivery: unknown field(s) {sorted(unknown)}")
    defaults = default_rules(default_environment, targets)

    triggers: tuple[Trigger, ...] = defaults.triggers
    if "triggers" in raw:
        items = raw["triggers"]
        if not isinstance(items, list) or len(items) > 50:
            raise DeliveryRuleError("delivery.triggers: must be a list of at most 50 rules")
        parsed: list[Trigger] = []
        for index, item in enumerate(items):
            where = f"delivery.triggers[{index}]"
            if not isinstance(item, dict):
                raise DeliveryRuleError(f"{where}: must be an object")
            on = item.get("on")
            if on not in TRIGGER_EVENTS:
                raise DeliveryRuleError(f"{where}.on: must be one of {sorted(TRIGGER_EVENTS)}")
            allowed = {"on", "tags" if on == "tag" else "branches", "deployTo", "registerVersion"}
            extra = set(item) - allowed
            if extra:
                raise DeliveryRuleError(f"{where}: unknown field(s) {sorted(extra)}")
            key = "tags" if on == "tag" else "branches"
            patterns = _patterns(item.get(key), f"{where}.{key}")
            deploy_to = None
            if item.get("deployTo") is not None:
                deploy_to = _environment(item["deployTo"], f"{where}.deployTo")
                if deploy_to not in AUTOMATIC_ENVIRONMENTS:
                    raise DeliveryRuleError(
                        f"{where}.deployTo: {deploy_to.value} is reached through a production "
                        "request and its approval, never by a trigger"
                    )
                if deploy_to not in targets:
                    raise DeliveryRuleError(f"{where}.deployTo: the module has no {deploy_to.value} target")
                if on == "pull_request":
                    # A pull request is unreviewed code. Deploying it to a shared
                    # environment replaces what everyone else is testing against.
                    raise DeliveryRuleError(f"{where}.deployTo: pull requests are built, not deployed")
            register = item.get("registerVersion", False)
            if not isinstance(register, bool):
                raise DeliveryRuleError(f"{where}.registerVersion: must be true or false")
            if register and on != "tag":
                raise DeliveryRuleError(f"{where}.registerVersion: only a tag names a version")
            parsed.append(Trigger(on, patterns, deploy_to=deploy_to, register_version=register))
        triggers = tuple(parsed)

    fork = raw.get("forkPullRequests", defaults.fork_pull_requests)
    if fork not in FORK_POLICIES:
        raise DeliveryRuleError(f"delivery.forkPullRequests: must be one of {sorted(FORK_POLICIES)}")

    promotion: dict[Environment, PromotionRule] = dict(defaults.promotion)
    if "promotion" in raw:
        block = raw["promotion"]
        if not isinstance(block, dict):
            raise DeliveryRuleError("delivery.promotion: must be an object")
        for env_name, rule in block.items():
            where = f"delivery.promotion.{env_name}"
            target = _environment(env_name, where)
            if target == Environment.DEV:
                raise DeliveryRuleError(f"{where}: dev is the first environment; nothing is promoted into it")
            if not isinstance(rule, dict) or set(rule) - {"requireHealthyIn", "minSoakMinutes"}:
                raise DeliveryRuleError(f"{where}: allowed fields are requireHealthyIn and minSoakMinutes")
            source = rule.get("requireHealthyIn")
            source_env = _environment(source, f"{where}.requireHealthyIn") if source is not None else None
            if source_env is not None and ENVIRONMENT_ORDER.index(source_env) >= ENVIRONMENT_ORDER.index(target):
                raise DeliveryRuleError(f"{where}.requireHealthyIn: must be an environment before {target.value}")
            if source_env is not None and source_env not in targets:
                raise DeliveryRuleError(f"{where}.requireHealthyIn: the module has no {source_env.value} target")
            soak = rule.get("minSoakMinutes", 0)
            if not isinstance(soak, int) or isinstance(soak, bool) or not 0 <= soak <= 10080:
                raise DeliveryRuleError(f"{where}.minSoakMinutes: must be an integer from 0 to 10080 (a week)")
            if soak and source_env is None:
                raise DeliveryRuleError(f"{where}.minSoakMinutes: a soak needs requireHealthyIn to say where")
            promotion[target] = PromotionRule(require_healthy_in=source_env, min_soak_minutes=soak)

    return DeliveryRules(triggers=triggers, fork_pull_requests=str(fork), promotion=promotion)


def decide(rules: DeliveryRules, event: ScmEvent) -> TriggerDecision:
    """The first matching rule wins, as in a firewall: order is the author's to choose."""

    if event.kind == "pull_request" and event.from_fork and rules.fork_pull_requests == "ignore":
        return TriggerDecision(False, "pull request from a fork; forkPullRequests is 'ignore'")
    value = (event.tag or "") if event.kind == "tag" else event.branch
    for index, trigger in enumerate(rules.triggers):
        if trigger.on != event.kind or not any(matches(p, value) for p in trigger.patterns):
            continue
        if event.kind == "pull_request" and event.from_fork:
            # Unreviewed code from outside the repository: it is built and tested, never
            # signed or published, so no artifact of it can be deployed or promoted, and
            # the signing key is never bound in its build.
            return TriggerDecision(True, f"rule {index}: fork pull request, verify only",
                                   publish=False, rule_index=index)
        release_tag = None
        if trigger.register_version and event.tag and SEMVER_TAG.fullmatch(event.tag):
            release_tag = event.tag
        what = f"deploy to {trigger.deploy_to.value}" if trigger.deploy_to else "build only"
        if trigger.register_version:
            what += f", register version {release_tag}" if release_tag else ", tag is not a version"
        return TriggerDecision(True, f"rule {index}: {what}", deploy_to=trigger.deploy_to,
                               release_tag=release_tag, rule_index=index)
    return TriggerDecision(False, f"no rule matches {event.kind} {value!r}")


@dataclass(frozen=True)
class SoakEvidence:
    proven_minutes: float
    deployment_id: str | None
    detail: str


def soak_evidence(
    *,
    digest: str,
    environment: Environment,
    deployments: Iterable[Deployment],
    now: datetime,
) -> SoakEvidence:
    """How long `digest` is known to have served `environment` without being rolled back.

    The clock starts at the deployment's `healthy_at` -- written once, by the transition
    to healthy, where `updated_at` moves on every later one -- and stops when the next
    deployment in that environment became healthy, or now. A deployment of the digest
    that was rolled back or failed proves nothing and its time is not counted: the soak
    is evidence that the release is fine.
    """

    in_env = [d for d in deployments if d.environment == environment]
    became_healthy = sorted(d.healthy_at for d in in_env if d.healthy_at is not None)
    best = SoakEvidence(0.0, None, f"{digest[:19]} has not been healthy in {environment.value}")
    for deployment in in_env:
        if deployment.artifact_digest != digest or deployment.status != DeploymentStatus.HEALTHY:
            continue
        started = deployment.healthy_at
        if started is None:
            continue
        ended = next((t for t in became_healthy if t > started), now)
        minutes = max(0.0, (ended - started).total_seconds() / 60)
        if best.deployment_id is None or minutes > best.proven_minutes:
            state = "serving now" if ended == now else "replaced afterwards"
            best = SoakEvidence(minutes, str(deployment.id),
                                f"healthy in {environment.value} for {minutes:.0f} min ({state})")
    return best


def rules_source(pipeline_config: Mapping[str, Any] | None) -> object:
    return (pipeline_config or {}).get("delivery")


PULL_REQUEST_REF = re.compile(r"^refs/(?:pull/\d{1,9}/head|merge-requests/\d{1,9}/head)$")


def pull_request_ref(value: object) -> str:
    """The pull request ref Jenkins may fetch, or nothing.

    This reaches a `git fetch` refspec, so it is the two shapes GitHub and GitLab use and
    nothing else -- not whatever a trigger record happens to hold.
    """

    text = str(value or "")
    return text if PULL_REQUEST_REF.fullmatch(text) else ""
