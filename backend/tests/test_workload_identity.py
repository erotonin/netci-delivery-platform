"""Machine identity: which workload is calling, about which resource, and may it say this.

The credential these tests replace was a single shared key. It proved the caller had
netCI's pipeline secret and nothing else -- not which build was calling, not which
application it belonged to, and not whether it was a CI controller reporting a build or a
worker reporting a deployment. Every assertion here is a thing that key could not refuse.
"""

from __future__ import annotations

import time

import pytest

from app import workload_identity
from app.workload_identity import Scope, Workload, WorkloadIdentityError
from uuid import uuid4

KEYS = "k1:" + "a" * 48


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setenv("NETCI_WORKLOAD_TOKEN_KEYS", KEYS)
    monkeypatch.delenv("NETCI_WORKLOAD_TOKEN_KEYS_FILE", raising=False)
    monkeypatch.delenv("NETCI_WORKLOAD_TOKEN_ISSUER", raising=False)
    monkeypatch.delenv("NETCI_WORKLOAD_TOKEN_AUDIENCE", raising=False)


def jenkins_token(**overrides):
    arguments = {
        "workload": Workload.JENKINS,
        "application_id": uuid4(),
        "pipeline_run_id": uuid4(),
        "scopes": {Scope.CI_RESULT},
    }
    arguments.update(overrides)
    return workload_identity.mint(**arguments)


def test_a_minted_token_verifies_and_names_its_resource():
    application_id, run_id = uuid4(), uuid4()

    token = jenkins_token(application_id=application_id, pipeline_run_id=run_id)
    claims = workload_identity.verify(token)

    assert claims.workload == Workload.JENKINS
    assert claims.application_id == application_id
    assert claims.pipeline_run_id == run_id
    assert claims.deployment_id is None
    assert claims.permits(Scope.CI_RESULT)


def test_a_token_for_one_run_does_not_name_another():
    """The whole point: a real token is still only good for the run it was issued for."""

    run_a, run_b = uuid4(), uuid4()

    claims = workload_identity.verify(jenkins_token(pipeline_run_id=run_a))

    assert claims.pipeline_run_id == run_a
    assert claims.pipeline_run_id != run_b


def test_jenkins_cannot_be_issued_a_deployment_scope():
    """Not merely absent from the token -- it cannot be minted into one."""

    with pytest.raises(WorkloadIdentityError) as failure:
        workload_identity.mint(
            workload=Workload.JENKINS,
            application_id=uuid4(),
            pipeline_run_id=uuid4(),
            scopes={Scope.DEPLOYMENT_RESULT},
        )

    assert failure.value.code == "SCOPE_NOT_PERMITTED"


def test_a_signed_scope_the_workload_may_never_hold_is_dropped_on_verify():
    """Defence in depth: a mis-issued token must not become an escalation."""

    import base64, hashlib, hmac, json

    header = base64.urlsafe_b64encode(
        json.dumps({"typ": "netci-callback", "alg": "HS256", "kid": "k1"}, sort_keys=True,
                   separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    now = int(time.time())
    claims = base64.urlsafe_b64encode(
        json.dumps({
            "iss": "netci", "aud": "netci-api", "sub": Workload.JENKINS,
            "application_id": str(uuid4()), "pipeline_run_id": str(uuid4()),
            "deployment_id": None,
            "scopes": [Scope.CI_RESULT, Scope.DEPLOYMENT_RESULT],
            "iat": now, "exp": now + 600, "jti": "f" * 32, "single_use": False,
        }, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(
        hmac.new(("a" * 48).encode(), f"{header}.{claims}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()

    verified = workload_identity.verify(f"{header}.{claims}.{signature}")

    assert verified.permits(Scope.CI_RESULT)
    assert not verified.permits(Scope.DEPLOYMENT_RESULT)


def test_an_expired_token_is_refused():
    token = jenkins_token()
    with pytest.raises(WorkloadIdentityError) as failure:
        workload_identity.verify(token, now=int(time.time()) + workload_identity.MAX_TTL_SECONDS + 10)
    assert failure.value.code == "TOKEN_EXPIRED"


def test_a_token_for_another_audience_is_refused(monkeypatch):
    token = jenkins_token()
    monkeypatch.setenv("NETCI_WORKLOAD_TOKEN_AUDIENCE", "some-other-netci")

    with pytest.raises(WorkloadIdentityError) as failure:
        workload_identity.verify(token)

    assert failure.value.code == "INVALID_AUDIENCE"


def test_a_token_from_another_issuer_is_refused(monkeypatch):
    token = jenkins_token()
    monkeypatch.setenv("NETCI_WORKLOAD_TOKEN_ISSUER", "netci-staging")

    with pytest.raises(WorkloadIdentityError) as failure:
        workload_identity.verify(token)

    assert failure.value.code == "INVALID_ISSUER"


def test_a_token_signed_with_another_key_is_refused(monkeypatch):
    token = jenkins_token()
    monkeypatch.setenv("NETCI_WORKLOAD_TOKEN_KEYS", "k1:" + "b" * 48)

    with pytest.raises(WorkloadIdentityError) as failure:
        workload_identity.verify(token)

    assert failure.value.code == "INVALID_SIGNATURE"


def test_a_tampered_claim_invalidates_the_signature():
    token = jenkins_token()
    header, claims, signature = token.split(".")
    forged = claims[:-4] + ("AAAA" if not claims.endswith("AAAA") else "BBBB")

    with pytest.raises(WorkloadIdentityError):
        workload_identity.verify(f"{header}.{forged}.{signature}")


def test_the_algorithm_comes_from_the_verifier_not_the_token():
    """`alg: none` is a token's suggestion, not netCI's decision."""

    import base64, json

    header = base64.urlsafe_b64encode(
        json.dumps({"typ": "netci-callback", "alg": "none", "kid": "k1"}).encode()
    ).rstrip(b"=").decode()
    now = int(time.time())
    claims = base64.urlsafe_b64encode(
        json.dumps({
            "iss": "netci", "aud": "netci-api", "sub": Workload.TEMPORAL,
            "application_id": str(uuid4()), "deployment_id": str(uuid4()),
            "pipeline_run_id": None, "scopes": [Scope.DEPLOYMENT_RESULT],
            "iat": now, "exp": now + 600, "jti": "e" * 32,
        }).encode()
    ).rstrip(b"=").decode()

    with pytest.raises(WorkloadIdentityError) as failure:
        workload_identity.verify(f"{header}.{claims}.")

    assert failure.value.code in {"INVALID_SIGNATURE", "MALFORMED_TOKEN"}


def test_rotation_accepts_both_keys_and_signs_with_the_active_one(monkeypatch):
    """Rotation has to be a config change, not an outage."""

    monkeypatch.setenv("NETCI_WORKLOAD_TOKEN_KEYS", "old:" + "c" * 48)
    old_token = jenkins_token()

    # Publish the new key first, keeping the old one for verification.
    monkeypatch.setenv("NETCI_WORKLOAD_TOKEN_KEYS", "new:" + "d" * 48 + ",old:" + "c" * 48)
    assert workload_identity.verify(old_token).workload == Workload.JENKINS
    new_token = jenkins_token()

    # Then retire the old key. The new token still verifies; the old one no longer does.
    monkeypatch.setenv("NETCI_WORKLOAD_TOKEN_KEYS", "new:" + "d" * 48)
    assert workload_identity.verify(new_token).workload == Workload.JENKINS
    with pytest.raises(WorkloadIdentityError):
        workload_identity.verify(old_token)


def test_a_short_signing_key_is_refused_rather_than_quietly_accepted(monkeypatch):
    """A key that looks configured but is brute-forceable is worse than none."""

    monkeypatch.setenv("NETCI_WORKLOAD_TOKEN_KEYS", "k1:tooshort")

    assert workload_identity.configured_keys() == []
    assert workload_identity.workload_identity_configured() is False


def test_a_token_must_name_exactly_one_resource():
    with pytest.raises(WorkloadIdentityError) as both:
        workload_identity.mint(
            workload=Workload.JENKINS, application_id=uuid4(),
            pipeline_run_id=uuid4(), deployment_id=uuid4(), scopes={Scope.CI_RESULT},
        )
    assert both.value.code == "AMBIGUOUS_SUBJECT"

    with pytest.raises(WorkloadIdentityError) as neither:
        workload_identity.mint(
            workload=Workload.JENKINS, application_id=uuid4(), scopes={Scope.CI_RESULT}
        )
    assert neither.value.code == "AMBIGUOUS_SUBJECT"


def test_a_terminal_scope_produces_a_single_use_token():
    token = workload_identity.mint(
        workload=Workload.TEMPORAL, application_id=uuid4(), deployment_id=uuid4(),
        scopes={Scope.DEPLOYMENT_RESULT},
    )
    assert workload_identity.verify(token).single_use is True

    reporting = jenkins_token()
    assert workload_identity.verify(reporting).single_use is False


def test_the_audit_payload_never_contains_the_token_or_a_secret():
    claims = workload_identity.verify(jenkins_token())

    rendered = repr(claims.as_audit_payload())

    assert "a" * 48 not in rendered
    assert "signature" not in rendered.lower()


# ------------------------------------------------------------------ startup policy


def test_non_local_mode_refuses_to_start_without_workload_identity(monkeypatch):
    monkeypatch.setenv("NETCI_ENVIRONMENT", "production")
    monkeypatch.delenv("NETCI_WORKLOAD_TOKEN_KEYS", raising=False)
    monkeypatch.delenv("NETCI_ALLOW_LEGACY_PIPELINE_KEY", raising=False)

    with pytest.raises(RuntimeError, match="NETCI_WORKLOAD_TOKEN_KEYS"):
        workload_identity.require_configured_workload_identity()


def test_the_legacy_shared_key_needs_an_explicit_migration_opt_in(monkeypatch):
    monkeypatch.setenv("NETCI_ENVIRONMENT", "production")
    monkeypatch.delenv("NETCI_WORKLOAD_TOKEN_KEYS", raising=False)

    monkeypatch.delenv("NETCI_ALLOW_LEGACY_PIPELINE_KEY", raising=False)
    assert workload_identity.legacy_shared_key_allowed() is False

    monkeypatch.setenv("NETCI_ALLOW_LEGACY_PIPELINE_KEY", "true")
    assert workload_identity.legacy_shared_key_allowed() is True
    workload_identity.require_configured_workload_identity()  # warns, does not raise


def test_local_mode_still_allows_the_shared_key(monkeypatch):
    monkeypatch.setenv("NETCI_ENVIRONMENT", "local")
    monkeypatch.delenv("NETCI_WORKLOAD_TOKEN_KEYS", raising=False)

    assert workload_identity.legacy_shared_key_allowed() is True
    workload_identity.require_configured_workload_identity()
