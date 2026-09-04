import pytest

from app.adapters.cd_orchestrator import build_cd_orchestrator
from app.adapters.ci_launcher import build_ci_launcher
from app.adapters.signature_verifier import build_signature_verifier
from app.auth import build_authenticator
from app.delivery import DeliveryPlatform


@pytest.mark.parametrize(
    "variable,builder",
    [
        ("NETCI_AUTH_MODE", build_authenticator),
        ("NETCI_CI_MODE", build_ci_launcher),
        ("NETCI_CD_MODE", build_cd_orchestrator),
        ("NETCI_SIGNATURE_VERIFY_MODE", build_signature_verifier),
    ],
)
def test_non_local_runtime_refuses_disabled_live_integrations(monkeypatch, variable, builder):
    monkeypatch.setenv("NETCI_ENVIRONMENT", "production")
    monkeypatch.setenv(variable, "none")

    with pytest.raises(RuntimeError, match=variable):
        builder()


def test_non_local_runtime_requires_supply_chain_evidence(monkeypatch):
    monkeypatch.setenv("NETCI_ENVIRONMENT", "production")
    monkeypatch.setenv("NETCI_REQUIRE_SECURITY_EVIDENCE", "false")

    with pytest.raises(RuntimeError, match="NETCI_REQUIRE_SECURITY_EVIDENCE"):
        DeliveryPlatform()


def test_local_runtime_keeps_explicit_none_mode_for_development(monkeypatch):
    monkeypatch.setenv("NETCI_ENVIRONMENT", "local")
    monkeypatch.setenv("NETCI_AUTH_MODE", "none")
    monkeypatch.setenv("NETCI_CI_MODE", "none")
    monkeypatch.setenv("NETCI_CD_MODE", "none")
    monkeypatch.setenv("NETCI_SIGNATURE_VERIFY_MODE", "none")
    monkeypatch.setenv("NETCI_REQUIRE_SECURITY_EVIDENCE", "false")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)

    assert build_authenticator().mode == "none"
    assert build_ci_launcher().mode == "none"
    assert build_cd_orchestrator().mode == "none"
    assert build_signature_verifier().mode == "none"
    assert DeliveryPlatform().security_evidence_required() is False
