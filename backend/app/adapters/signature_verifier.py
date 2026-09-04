"""Seam for re-verifying an artifact's signature at deploy time.

The supply-chain policy checks `signature.verified` — a boolean CI wrote about its own
work. That is a reasonable record and a poor control: it says the build *believed* the
artifact was signed. If the CI system is compromised, or a build script is edited, or
evidence is replayed from a different pipeline, the boolean still reads `true`.

Verifying again here closes that gap. netCI re-runs `cosign verify` against the digest it
is about to deploy, using a public key netCI holds, at the moment of deployment — minutes
or hours after the build, on a different host, with a different trust root.

`NETCI_SIGNATURE_VERIFY_MODE` selects the implementation:

    none    trust the recorded evidence. The default, because verification needs a public
            key and a reachable artifact, and a platform that fails every deployment on a
            missing key is not safer, only broken.
    cosign  run cosign against the artifact. Fails closed: an artifact that cannot be
            verified is not deployed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from ..runtime_environment import require_live_mode

logger = logging.getLogger(__name__)

DIGEST_PREFIX = "sha256:"


class SignatureVerificationError(RuntimeError):
    """The artifact's signature could not be verified against the key netCI holds."""


@dataclass(frozen=True)
class ArtifactIdentity:
    """What netCI is about to deploy, as it will be verified.

    `reference` is whatever CI recorded as `artifactRef`: an OCI reference for container
    and Kubernetes runtimes, a `file://` path for a signed systemd binary.
    """

    digest: str
    reference: str | None
    bundle_location: str | None = None

    @property
    def is_oci(self) -> bool:
        return bool(self.reference) and not str(self.reference).startswith("file://")

    def pinned_reference(self) -> str:
        """The reference with the tag replaced by the digest.

        Verifying `myapp:latest` proves something about whatever that tag points at now,
        which is not necessarily what is being deployed. Pinning to the digest is the
        whole point of an immutable artifact.
        """

        reference = str(self.reference or "")
        if "@" in reference:
            reference = reference.split("@", 1)[0]
        # Strip a tag, but not the port in a registry host (`localhost:5000/app:1.2`).
        head, separator, tail = reference.rpartition(":")
        if separator and "/" not in tail:
            reference = head
        return f"{reference}@{self.digest}"


class SignatureVerifier(Protocol):
    mode: str

    async def verify(self, artifact: ArtifactIdentity) -> str: ...


class NullSignatureVerifier:
    """Accept the evidence CI recorded, without re-checking it.

    Honest about what it is: this is a record, not a verification. `/healthz` reports the
    mode so an operator can tell which one a deployment is relying on.
    """

    mode = "none"

    async def verify(self, artifact: ArtifactIdentity) -> str:
        return "signature not re-verified at deploy time (NETCI_SIGNATURE_VERIFY_MODE=none)"


def _read_key() -> str:
    """The public key netCI verifies with, from a file or an inline value.

    A *public* key, deliberately: netCI never needs the signing key, and holding one would
    make the deployment host able to mint signatures it is supposed to be checking.
    """

    path = os.getenv("NETCI_COSIGN_PUBLIC_KEY_FILE", "").strip()
    if path:
        return path
    inline = os.getenv("NETCI_COSIGN_PUBLIC_KEY", "").strip()
    if not inline:
        raise SignatureVerificationError(
            "NETCI_SIGNATURE_VERIFY_MODE=cosign requires NETCI_COSIGN_PUBLIC_KEY_FILE "
            "(preferred) or NETCI_COSIGN_PUBLIC_KEY"
        )
    return inline


class CosignSignatureVerifier:
    """Re-verify with cosign, against the digest being deployed.

    Fails closed on every path that is not a clean verification: a missing binary, a
    missing key, an unreachable artifact, a non-zero exit, or a timeout.
    """

    mode = "cosign"

    def __init__(
        self,
        *,
        executable: str = "cosign",
        key: str | None = None,
        timeout_seconds: float = 60.0,
        allow_unpinned: bool = False,
        require_tlog: bool = False,
    ) -> None:
        self.executable = executable
        self._key = key
        self.timeout_seconds = timeout_seconds
        # Must match how the artifact was signed. The checked-in CI scripts sign with
        # `--tlog-upload=false`, which is right for a local or air-gapped registry, and
        # cosign then refuses to verify unless told the same -- it fails with "signature
        # not found in transparency log", which reads like a bad signature but is a
        # configuration mismatch. A deployment with a real Rekor should set this true, and
        # then a signature that was never logged is correctly refused.
        self.require_tlog = require_tlog
        # An artifact with no recorded reference cannot be located, let alone verified.
        # Off by default: "we could not find it, so we shipped it" is the failure this
        # whole module exists to prevent.
        self.allow_unpinned = allow_unpinned

    def key(self) -> str:
        return self._key if self._key is not None else _read_key()

    async def _run(self, command: list[str]) -> tuple[int, str]:
        if shutil.which(self.executable) is None:
            raise SignatureVerificationError(
                f"{self.executable} is not installed on the deployment host, so the "
                "artifact signature cannot be re-verified"
            )
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            process.kill()
            # Reap it, but do not block on it. An unawaited killed child leaves a transport
            # for the event loop to close after the loop is gone; waiting without a bound
            # would in turn hang the deployment whenever cosign left a grandchild holding
            # the pipe. Neither failure mode is acceptable on a deploy path.
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("cosign did not exit after being killed; abandoning the process")
            raise SignatureVerificationError(
                f"cosign did not finish within {self.timeout_seconds:.0f}s"
            ) from exc
        return process.returncode or 0, (stdout or b"").decode(errors="replace")

    async def verify(self, artifact: ArtifactIdentity) -> str:
        if not artifact.digest.startswith(DIGEST_PREFIX):
            raise SignatureVerificationError(
                f"refusing to verify a non-digest artifact identity: {artifact.digest!r}"
            )
        if not artifact.reference:
            if self.allow_unpinned:
                return "artifact has no recorded reference; verification skipped by configuration"
            raise SignatureVerificationError(
                "the artifact has no recorded reference, so netCI cannot locate it to "
                "verify its signature. CI must publish `artifactRef` alongside the digest."
            )

        key = self.key()
        tlog = [] if self.require_tlog else ["--insecure-ignore-tlog=true"]
        if artifact.is_oci:
            target = artifact.pinned_reference()
            command = [self.executable, "verify", "--key", key, *tlog, target]
        else:
            if not artifact.bundle_location:
                raise SignatureVerificationError(
                    "a blob artifact needs a signature bundle to verify against; CI must "
                    "publish `signature.bundleLocation`"
                )
            blob = urlsplit(str(artifact.reference)).path
            if not Path(blob).is_file():
                raise SignatureVerificationError(
                    f"the signed artifact is not reachable from this host: {blob}"
                )
            target = blob
            command = [
                self.executable, "verify-blob",
                "--key", key,
                "--bundle", str(artifact.bundle_location),
                *tlog,
                blob,
            ]

        code, output = await self._run(command)
        if code != 0:
            # The tail, not the whole log: enough to act on, without pasting a page of
            # cosign output into a deployment record.
            raise SignatureVerificationError(
                f"cosign could not verify {target}: {output.strip()[-600:] or 'no output'}"
            )
        logger.info("verified artifact signature for %s", target)
        return f"cosign verified {target} against the netCI public key"


def build_signature_verifier() -> SignatureVerifier:
    """Select the verifier from configuration. Called once, where the worker is composed."""

    mode = os.getenv("NETCI_SIGNATURE_VERIFY_MODE", "none").strip().lower()
    require_live_mode("NETCI_SIGNATURE_VERIFY_MODE", mode, disabled={"", "none"})
    if mode in {"", "none"}:
        return NullSignatureVerifier()
    if mode == "cosign":
        return CosignSignatureVerifier(
            executable=os.getenv("NETCI_COSIGN_EXECUTABLE", "cosign").strip() or "cosign",
            timeout_seconds=float(os.getenv("NETCI_SIGNATURE_VERIFY_TIMEOUT", "60")),
            allow_unpinned=os.getenv("NETCI_SIGNATURE_ALLOW_UNPINNED", "false").strip().lower()
            in {"true", "1", "yes"},
            require_tlog=os.getenv("NETCI_SIGNATURE_REQUIRE_TLOG", "false").strip().lower()
            in {"true", "1", "yes"},
        )
    raise ValueError(f"NETCI_SIGNATURE_VERIFY_MODE must be none or cosign (got {mode!r})")
