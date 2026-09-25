"""Re-verifying the artifact signature at deploy time.

The supply-chain policy checks `signature.verified`, which is a boolean CI wrote about its
own work. It records that the build believed the artifact was signed; it does not prove it
now. These tests are about the difference — most importantly the one at the bottom, where
evidence says `verified: true` and the deployment is refused anyway because the signature
does not actually check out.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from app.adapters.signature_verifier import (
    ArtifactIdentity,
    CosignSignatureVerifier,
    NullSignatureVerifier,
    SignatureVerificationError,
    build_signature_verifier,
)
from app.policy.rules import PolicyViolation
from app.workflows.activities import DeliveryActivities, FileEvidenceStore
from app.workflows.provision_and_deploy import DeliveryInput


DIGEST = "sha256:" + "a" * 64


def fake_cosign(tmp_path: Path, *, exit_code: int = 0, output: str = "Verified OK", name: str = "cosign") -> Path:
    """A stand-in for the cosign binary that records how it was invoked.

    A fake executable rather than a patched function: the command line, the exit code and
    the process plumbing are the parts that go wrong, so they are the parts worth running.
    """

    script = tmp_path / name
    log = tmp_path / "cosign-args.txt"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        f'echo "{output}"\n'
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def invocations(tmp_path: Path) -> list[str]:
    log = tmp_path / "cosign-args.txt"
    return log.read_text(encoding="utf-8").strip().splitlines() if log.is_file() else []


def verifier(tmp_path: Path, **overrides) -> CosignSignatureVerifier:
    # The default binary is created only when the caller did not supply one: both write
    # into tmp_path, so building it eagerly would overwrite the caller's fake.
    options = {"key": "/keys/netci.pub", **overrides}
    options.setdefault("executable", str(fake_cosign(tmp_path)))
    return CosignSignatureVerifier(**options)


# ------------------------------------------------------------------ pinning the target


@pytest.mark.parametrize(
    "reference, expected",
    [
        ("registry.local/netci/app:1.4.2", f"registry.local/netci/app@{DIGEST}"),
        ("registry.local/netci/app", f"registry.local/netci/app@{DIGEST}"),
        # A registry port is not a tag.
        ("localhost:55000/netci/app:latest", f"localhost:55000/netci/app@{DIGEST}"),
        ("localhost:55000/netci/app", f"localhost:55000/netci/app@{DIGEST}"),
        # An already-pinned reference is re-pinned to the digest being deployed, never
        # trusted to already name the right one.
        (f"registry.local/netci/app@sha256:{'b' * 64}", f"registry.local/netci/app@{DIGEST}"),
    ],
)
def test_the_target_is_always_the_digest_never_the_tag(reference, expected):
    """Verifying `app:latest` proves something about whatever that tag points at now."""

    assert ArtifactIdentity(digest=DIGEST, reference=reference).pinned_reference() == expected


# ------------------------------------------------------------------------ the happy path


@pytest.mark.asyncio
async def test_an_oci_artifact_is_verified_against_its_digest(tmp_path):
    outcome = await verifier(tmp_path).verify(
        ArtifactIdentity(digest=DIGEST, reference="registry.local/netci/app:1.4.2")
    )

    assert "cosign verified" in outcome
    command = invocations(tmp_path)[0]
    # The tlog flag matches how the checked-in CI scripts sign (`--tlog-upload=false`);
    # without it cosign refuses with "signature not found in transparency log".
    assert command == (
        f"verify --key /keys/netci.pub --insecure-ignore-tlog=true registry.local/netci/app@{DIGEST}"
    )


@pytest.mark.asyncio
async def test_a_signed_blob_is_verified_against_its_bundle(tmp_path):
    blob = tmp_path / "hello-systemd-v0.1.0"
    blob.write_bytes(b"binary")
    bundle = tmp_path / "signature.bundle.json"
    bundle.write_text("{}", encoding="utf-8")

    await verifier(tmp_path).verify(
        ArtifactIdentity(digest=DIGEST, reference=f"file://{blob}", bundle_location=str(bundle))
    )

    assert invocations(tmp_path)[0] == (
        f"verify-blob --key /keys/netci.pub --bundle {bundle} --insecure-ignore-tlog=true {blob}"
    )


@pytest.mark.asyncio
async def test_requiring_a_transparency_log_drops_the_ignore_flag(tmp_path):
    """A deployment with a real Rekor must actually demand the log entry."""

    strict = verifier(tmp_path, require_tlog=True, rekor_url="https://rekor.corp.example")
    await strict.verify(ArtifactIdentity(digest=DIGEST, reference="registry.local/netci/app:1.4.2"))
    assert "--insecure-ignore-tlog" not in invocations(tmp_path)[0]
    # ...and against the log the build uploaded to, not cosign's public default.
    assert "--rekor-url https://rekor.corp.example" in invocations(tmp_path)[0]


# ----------------------------------------------------------------- failing closed


@pytest.mark.asyncio
async def test_a_signature_that_does_not_check_out_is_refused(tmp_path):
    failing = verifier(tmp_path, executable=str(fake_cosign(tmp_path, exit_code=1, output="no matching signatures", name="cosign-bad")))
    with pytest.raises(SignatureVerificationError, match="no matching signatures"):
        await failing.verify(ArtifactIdentity(digest=DIGEST, reference="registry.local/netci/app:1.4.2"))


@pytest.mark.asyncio
async def test_a_missing_cosign_binary_fails_closed(tmp_path):
    absent = CosignSignatureVerifier(executable=str(tmp_path / "not-installed"), key="/keys/netci.pub")
    with pytest.raises(SignatureVerificationError, match="not installed"):
        await absent.verify(ArtifactIdentity(digest=DIGEST, reference="registry.local/netci/app:1.4.2"))


@pytest.mark.asyncio
async def test_an_artifact_with_no_reference_cannot_be_verified_so_it_is_refused(tmp_path):
    """"We could not find it, so we shipped it" is the outcome this module exists to prevent."""

    with pytest.raises(SignatureVerificationError, match="no recorded reference"):
        await verifier(tmp_path).verify(ArtifactIdentity(digest=DIGEST, reference=None))


@pytest.mark.asyncio
async def test_skipping_an_unlocatable_artifact_must_be_asked_for_explicitly(tmp_path):
    lenient = verifier(tmp_path, allow_unpinned=True)
    outcome = await lenient.verify(ArtifactIdentity(digest=DIGEST, reference=None))
    assert "skipped by configuration" in outcome


@pytest.mark.asyncio
async def test_a_non_digest_identity_is_refused_before_cosign_runs(tmp_path):
    with pytest.raises(SignatureVerificationError, match="non-digest"):
        await verifier(tmp_path).verify(ArtifactIdentity(digest="latest", reference="app:latest"))
    assert invocations(tmp_path) == []


@pytest.mark.asyncio
async def test_a_blob_with_no_bundle_or_no_file_is_refused(tmp_path):
    blob = tmp_path / "binary"
    blob.write_bytes(b"x")

    with pytest.raises(SignatureVerificationError, match="signature bundle"):
        await verifier(tmp_path).verify(ArtifactIdentity(digest=DIGEST, reference=f"file://{blob}"))

    with pytest.raises(SignatureVerificationError, match="not reachable"):
        await verifier(tmp_path).verify(
            ArtifactIdentity(
                digest=DIGEST, reference=f"file://{tmp_path / 'gone'}", bundle_location=str(tmp_path)
            )
        )


@pytest.mark.asyncio
async def test_cosign_mode_without_a_public_key_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("NETCI_COSIGN_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("NETCI_COSIGN_PUBLIC_KEY_FILE", raising=False)
    keyless = CosignSignatureVerifier(executable=str(fake_cosign(tmp_path)))
    with pytest.raises(SignatureVerificationError, match="NETCI_COSIGN_PUBLIC_KEY"):
        await keyless.verify(ArtifactIdentity(digest=DIGEST, reference="registry.local/app:1"))


@pytest.mark.asyncio
async def test_a_hanging_cosign_does_not_hang_the_deployment(tmp_path):
    slow = tmp_path / "cosign-slow"
    # `exec` so the process netCI kills *is* the sleep. Without it the shell dies and the
    # orphaned sleep keeps the stdout pipe open, which is a 30-second test and, in
    # production, a deployment that hangs on a subprocess nobody is waiting for.
    slow.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    slow.chmod(slow.stat().st_mode | stat.S_IEXEC)
    stalling = CosignSignatureVerifier(executable=str(slow), key="/keys/netci.pub", timeout_seconds=0.5)

    with pytest.raises(SignatureVerificationError, match="did not finish"):
        await stalling.verify(ArtifactIdentity(digest=DIGEST, reference="registry.local/app:1"))


# ------------------------------------------------------------------ composition root


def test_the_default_is_to_trust_the_recorded_evidence(monkeypatch):
    monkeypatch.delenv("NETCI_SIGNATURE_VERIFY_MODE", raising=False)
    built = build_signature_verifier()
    assert isinstance(built, NullSignatureVerifier)
    assert built.mode == "none"


def test_an_unknown_mode_is_a_configuration_error_not_a_silent_downgrade(monkeypatch):
    monkeypatch.setenv("NETCI_SIGNATURE_VERIFY_MODE", "probably")
    with pytest.raises(ValueError, match="must be none or cosign"):
        build_signature_verifier()


# --------------------------------------------------- the gap this closes, end to end


def delivery() -> DeliveryInput:
    return DeliveryInput(
        application_id="app-1",
        pipeline_run_id="run-1",
        runtime="docker",
        environment="prod",
        artifact_digest=DIGEST,
        parameters={},
    )


def write_evidence(root: Path, *, reference: str | None = "registry.local/netci/app:1.4.2") -> None:
    """Evidence a compromised or mistaken CI could produce: it claims a valid signature."""

    root.mkdir(parents=True, exist_ok=True)
    (root / "run-1.json").write_text(
        json.dumps(
            {
                "applicationId": "app-1",
                "artifactDigest": DIGEST,
                "artifactRef": reference,
                "sbom": {"format": "cyclonedx-json", "location": "s3://netci/sbom.json", "generatedBy": "syft"},
                "vulnerabilityScan": {"scanner": "trivy", "status": "passed", "critical": 0, "high": 0},
                # The claim under test.
                "signature": {"provider": "cosign", "verified": True},
                "decision": "allow",
                "recordedAt": "2026-08-28T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )


class UnusedRuntime:
    async def deploy(self, delivery):  # pragma: no cover - must never be reached
        raise AssertionError("deployment proceeded despite an unverifiable signature")

    async def health_check(self, delivery):  # pragma: no cover
        return True

    async def rollback(self, delivery):  # pragma: no cover
        return None


@pytest.mark.asyncio
async def test_evidence_claiming_a_valid_signature_is_not_enough(tmp_path):
    """The whole point: `verified: true` in evidence does not deploy an unverifiable artifact."""

    evidence_dir = tmp_path / "evidence"
    write_evidence(evidence_dir)
    activities = DeliveryActivities(
        FileEvidenceStore(evidence_dir),
        UnusedRuntime(),
        verifier(tmp_path, executable=str(fake_cosign(tmp_path, exit_code=1, output="no matching signatures", name="cosign-bad"))),
    )

    with pytest.raises(PolicyViolation, match="signature re-verification failed"):
        await activities.validate_artifact(delivery())


@pytest.mark.asyncio
async def test_a_genuinely_verifiable_artifact_still_deploys(tmp_path):
    evidence_dir = tmp_path / "evidence"
    write_evidence(evidence_dir)
    activities = DeliveryActivities(FileEvidenceStore(evidence_dir), UnusedRuntime(), verifier(tmp_path))

    await activities.validate_artifact(delivery())
    assert invocations(tmp_path)[0].endswith(f"registry.local/netci/app@{DIGEST}")


@pytest.mark.asyncio
async def test_the_default_worker_still_deploys_without_a_key_configured(tmp_path):
    """Turning verification on is a decision; not having made it must not break delivery."""

    evidence_dir = tmp_path / "evidence"
    write_evidence(evidence_dir)
    activities = DeliveryActivities(FileEvidenceStore(evidence_dir), UnusedRuntime())
    await activities.validate_artifact(delivery())


# ------------------------------------------------------- against cosign itself


def _cosign_available() -> bool:
    return shutil.which("cosign") is not None


@pytest.mark.skipif(not _cosign_available(), reason="cosign is not installed on this host")
@pytest.mark.asyncio
async def test_real_cosign_accepts_a_genuine_signature_and_refuses_everything_else(tmp_path):
    """The fake binary proves the plumbing; only real cosign proves the verification.

    It also caught a defect the fake could not: netCI's own CI scripts sign with
    `--tlog-upload=false`, and cosign then refuses to verify unless told the same. The
    verifier would have rejected every artifact this platform produces.
    """

    environment = {**os.environ, "COSIGN_PASSWORD": ""}
    subprocess.run(["cosign", "generate-key-pair"], cwd=tmp_path, env=environment, check=True, capture_output=True)

    blob = tmp_path / "artifact.bin"
    blob.write_bytes(b"hello netci\n")
    bundle = tmp_path / "signature.bundle.json"
    sign = ["cosign", "sign-blob", "--yes", "--key", "cosign.key", "--bundle", str(bundle), "--tlog-upload=false"]
    if b"--use-signing-config" in subprocess.run(
        ["cosign", "sign-blob", "--help"], capture_output=True
    ).stdout:
        sign.append("--use-signing-config=false")
    subprocess.run([*sign, str(blob)], cwd=tmp_path, env=environment, check=True, capture_output=True)

    digest = "sha256:" + hashlib.sha256(blob.read_bytes()).hexdigest()
    verify = CosignSignatureVerifier(key=str(tmp_path / "cosign.pub"))
    identity = ArtifactIdentity(digest=digest, reference=f"file://{blob}", bundle_location=str(bundle))

    assert "cosign verified" in await verify.verify(identity)

    # Same bundle, different bytes.
    tampered = tmp_path / "tampered.bin"
    tampered.write_bytes(b"hello netci\n\x00evil")
    with pytest.raises(SignatureVerificationError):
        await verify.verify(
            ArtifactIdentity(digest=digest, reference=f"file://{tampered}", bundle_location=str(bundle))
        )

    # Right bytes, wrong key.
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["cosign", "generate-key-pair"], cwd=other, env=environment, check=True, capture_output=True)
    with pytest.raises(SignatureVerificationError):
        await CosignSignatureVerifier(key=str(other / "cosign.pub")).verify(identity)

    # Demanding a transparency-log entry the signature never got is also a refusal, so
    # `require_tlog` is a real setting rather than a decorative one.
    with pytest.raises(SignatureVerificationError):
        await CosignSignatureVerifier(key=str(tmp_path / "cosign.pub"), require_tlog=True,
                                      rekor_url="https://rekor.corp.example").verify(identity)
