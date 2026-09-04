import json
from pathlib import Path

import pytest
import yaml

from app.policy.rules import PolicyViolation
from app.workflows.activities import (
    AnsibleRuntimeRunner,
    DeliveryActivities,
    FileEvidenceStore,
)
from app.workflows.provision_and_deploy import DeliveryInput


class FakeRuntimeRunner:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy
        self.deployed: list[DeliveryInput] = []
        self.rolled_back: list[DeliveryInput] = []

    async def deploy(self, delivery: DeliveryInput) -> str:
        self.deployed.append(delivery)
        return f"deployment-{delivery.pipeline_run_id}"

    async def health_check(self, delivery: DeliveryInput) -> bool:
        return self.healthy

    async def rollback(self, delivery: DeliveryInput) -> None:
        self.rolled_back.append(delivery)


def delivery() -> DeliveryInput:
    return DeliveryInput(
        application_id="app-1",
        pipeline_run_id="run-1",
        runtime="docker",
        environment="staging",
        artifact_digest="sha256:" + "a" * 64,
        parameters={
            "artifact_ref": "registry.local/hello@sha256:" + "a" * 64,
            "target_hosts": ["app-01"],
        },
    )


def write_evidence(root: Path, *, decision: str = "allow", digest: str | None = None) -> None:
    item = delivery()
    payload = {
        "applicationId": item.application_id,
        "artifactDigest": digest or item.artifact_digest,
        "sbom": {"format": "cyclonedx-json", "location": "s3://netci/sbom.json", "generatedBy": "syft"},
        "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
        "signature": {"provider": "cosign", "verified": True},
        "decision": decision,
        "recordedAt": "2026-08-25T00:00:00Z",
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "run-1.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_delivery_activities_validate_evidence_and_delegate_runtime(tmp_path: Path):
    write_evidence(tmp_path)
    runtime = FakeRuntimeRunner()
    activities = DeliveryActivities(FileEvidenceStore(tmp_path), runtime)

    await activities.validate_artifact(delivery())
    result = await activities.deploy(delivery())

    assert result.status == "deploying"
    assert result.deployment_id == "deployment-run-1"
    assert await activities.health_check(delivery()) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,digest", [("deny", None), ("allow", "sha256:" + "b" * 64)])
async def test_delivery_activities_fail_closed_on_invalid_evidence(tmp_path: Path, decision: str, digest: str | None):
    write_evidence(tmp_path, decision=decision, digest=digest)
    activities = DeliveryActivities(FileEvidenceStore(tmp_path), FakeRuntimeRunner())

    with pytest.raises(PolicyViolation):
        await activities.validate_artifact(delivery())


def test_ansible_runner_builds_runtime_specific_immutable_command(tmp_path: Path):
    runner = AnsibleRuntimeRunner(project_root=tmp_path, inventory=tmp_path / "inventory.ini")

    command = runner.command_for("deploy", delivery())

    assert command[:3] == ["ansible-playbook", "-i", str(tmp_path / "inventory.ini")]
    assert command[3].endswith("deploy-docker.yml")
    extra_vars = json.loads(command[-1])
    assert extra_vars["artifact_digest"] == delivery().artifact_digest
    assert extra_vars["artifact_ref"].endswith(delivery().artifact_digest)
    assert command[4:6] == ["--limit", "app-01"]


def test_ansible_runner_never_allows_parameters_to_override_verified_identity(tmp_path: Path):
    runner = AnsibleRuntimeRunner(project_root=tmp_path, inventory=tmp_path / "inventory.ini")
    item = delivery()
    poisoned = DeliveryInput(
        **{
            **item.__dict__,
            "deployment_id": "deployment-trusted",
            "release_name": "release-trusted",
            "parameters": {
                **item.parameters,
                "application_id": "attacker-application",
                "pipeline_run_id": "attacker-run",
                "target_environment": "prod",
                "artifact_digest": "sha256:" + "b" * 64,
                "release_name": "attacker-release",
            },
        }
    )

    command = runner.command_for("deploy", poisoned)
    extra_vars = json.loads(command[-1])

    assert extra_vars["application_id"] == item.application_id
    assert extra_vars["pipeline_run_id"] == item.pipeline_run_id
    assert extra_vars["deployment_id"] == "deployment-trusted"
    assert extra_vars["target_environment"] == item.environment
    assert extra_vars["artifact_digest"] == item.artifact_digest
    assert extra_vars["release_name"] == "release-trusted"


def test_ansible_runner_refuses_missing_or_unsafe_host_scope(tmp_path: Path):
    runner = AnsibleRuntimeRunner(project_root=tmp_path, inventory=tmp_path / "inventory.ini")
    item = delivery()

    with pytest.raises(ValueError, match="explicit target_hosts"):
        runner.command_for("deploy", DeliveryInput(**{**item.__dict__, "parameters": {}}))
    with pytest.raises(ValueError, match="safe inventory host"):
        runner.command_for(
            "deploy",
            DeliveryInput(**{**item.__dict__, "parameters": {"target_hosts": ["all:!protected"]}}),
        )


def test_ansible_ssh_material_must_be_real_files_under_the_secret_root(tmp_path: Path, monkeypatch):
    runner = AnsibleRuntimeRunner(project_root=tmp_path, inventory=tmp_path / "inventory.ini")
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    key = secret_root / "id_ed25519"
    key.write_text("test-key", encoding="utf-8")
    known_hosts = secret_root / "known_hosts"
    known_hosts.write_text("app-01 ssh-ed25519 test-key", encoding="utf-8")
    monkeypatch.setenv("NETCI_ANSIBLE_SECRET_DIR", str(secret_root))
    monkeypatch.setenv("NETCI_ANSIBLE_PRIVATE_KEY_FILE", str(key))
    monkeypatch.setenv("NETCI_ANSIBLE_KNOWN_HOSTS_FILE", str(known_hosts))

    command = runner.command_for("deploy", delivery())

    assert command[3:5] == ["--private-key", str(key)]
    assert command[5] == "--ssh-common-args"
    assert f"UserKnownHostsFile={known_hosts}" in command[6]

    monkeypatch.setenv("NETCI_ANSIBLE_PRIVATE_KEY_FILE", str(tmp_path / "outside-key"))
    with pytest.raises(ValueError, match="beneath NETCI_ANSIBLE_SECRET_DIR"):
        runner.command_for("deploy", delivery())


@pytest.mark.asyncio
async def test_managed_playbook_success_is_the_runtime_health_gate(tmp_path: Path):
    runner = AnsibleRuntimeRunner(project_root=tmp_path, inventory=tmp_path / "inventory.ini")
    item = delivery()
    managed = DeliveryInput(
        **{**item.__dict__, "parameters": {**item.parameters, "runtime_health_verified": True}}
    )

    assert await runner.health_check(managed) is True


def test_kubernetes_playbook_uses_explicit_kubeconfig_and_namespace():
    project_root = Path(__file__).resolve().parents[2]
    playbook = yaml.safe_load(
        (project_root / "deploy/ansible/playbooks/deploy-kubernetes.yml").read_text(encoding="utf-8")
    )[0]
    tasks = playbook["tasks"]
    helm = next(task["kubernetes.core.helm"] for task in tasks if "kubernetes.core.helm" in task)
    workload = next(task["kubernetes.core.k8s_info"] for task in tasks if "kubernetes.core.k8s_info" in task)

    assert "kubeconfig" in helm
    assert "kubeconfig" in workload
    assert helm["release_namespace"] == "{{ netci_target_namespace }}"
    assert workload["namespace"] == "{{ netci_target_namespace }}"
