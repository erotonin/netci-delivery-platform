"""Fetch a non-image artifact that CI stored in the OCI registry.

netCI has one artifact store: the registry. A container image is pushed as an image; a
Linux binary is pushed as a one-layer OCI artifact (`cosign upload blob`), signed with
the same key and verified with the same `cosign verify` as an image. That gives every
artifact the same identity -- the manifest digest -- and the same signature gate, and
it means the systemd path needs no second store, no pre-signed upload URLs and no shared
filesystem between the build agent and the worker.

The worker fetches the blob just before the playbook runs. Everything is content
addressed and checked twice: the manifest bytes must hash to the digest that was
verified, and the blob bytes must hash to the layer digest the manifest names. A registry
that served something else is caught here, not on the target.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_REFERENCE = re.compile(r"^(?P<host>[A-Za-z0-9.-]+(?::[0-9]{1,5})?)/(?P<repository>[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*)@(?P<digest>sha256:[0-9a-f]{64})$")
_MANIFEST_TYPES = "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"
MAX_BLOB_BYTES = 2 * 1024 * 1024 * 1024


class OciBlobError(RuntimeError):
    """The artifact could not be fetched or did not match its digest. Fail closed."""


@dataclass(frozen=True)
class FetchedBlob:
    path: Path
    sha256: str
    size: int
    media_type: str


def _loopback(host: str) -> bool:
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host
    return name in {"localhost", "127.0.0.1", "::1"}


def _get(url: str, *, accept: str | None = None, timeout: float) -> tuple[bytes, dict[str, str]]:
    request = urllib.request.Request(url, headers={"Accept": accept} if accept else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_BLOB_BYTES + 1)
            if len(body) > MAX_BLOB_BYTES:
                raise OciBlobError(f"artifact larger than {MAX_BLOB_BYTES} bytes refused")
            return body, {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        raise OciBlobError(f"registry answered HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise OciBlobError(f"registry unreachable: {exc.reason}") from exc


def fetch_blob(reference: str, destination_dir: Path, *, allow_http: bool = False, timeout: float = 60.0) -> FetchedBlob:
    """Download the single layer of `host/repo@sha256:...` into `destination_dir`.

    Plain HTTP is used only for loopback hosts or when the caller says the registry is
    known to be plaintext (a lab); a production registry speaks TLS and the fallback
    never triggers.
    """

    match = _REFERENCE.fullmatch(reference.strip())
    if match is None:
        raise OciBlobError(f"not a digest-pinned OCI reference: {reference!r}")
    host, repository, digest = match["host"], match["repository"], match["digest"]
    schemes = ["https", "http"] if (allow_http or _loopback(host)) else ["https"]

    last_error: OciBlobError | None = None
    for scheme in schemes:
        base = f"{scheme}://{host}/v2/{repository}"
        try:
            manifest_bytes, _ = _get(f"{base}/manifests/{digest}", accept=_MANIFEST_TYPES, timeout=timeout)
        except OciBlobError as exc:
            last_error = exc
            continue
        actual = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        if actual != digest:
            raise OciBlobError(f"manifest served for {digest} hashes to {actual}; refusing")
        try:
            manifest = json.loads(manifest_bytes)
        except ValueError as exc:
            raise OciBlobError("manifest is not JSON") from exc
        layers = manifest.get("layers") if isinstance(manifest, dict) else None
        if not isinstance(layers, list) or len(layers) != 1 or not isinstance(layers[0], dict):
            raise OciBlobError("a blob artifact has exactly one layer; this manifest does not")
        layer = layers[0]
        layer_digest = str(layer.get("digest") or "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", layer_digest):
            raise OciBlobError(f"layer digest is not sha256: {layer_digest!r}")
        expected_size = int(layer.get("size") or 0)
        if expected_size <= 0 or expected_size > MAX_BLOB_BYTES:
            raise OciBlobError(f"layer size {expected_size} is not acceptable")

        blob_bytes, _ = _get(f"{base}/blobs/{layer_digest}", timeout=timeout)
        blob_sha = hashlib.sha256(blob_bytes).hexdigest()
        if "sha256:" + blob_sha != layer_digest or len(blob_bytes) != expected_size:
            raise OciBlobError(
                f"blob for {layer_digest} hashes to sha256:{blob_sha} ({len(blob_bytes)} bytes); refusing"
            )
        destination_dir.mkdir(parents=True, exist_ok=True)
        # Named by content: two workers, or two runs, fetching the same artifact write
        # the same bytes to the same path, and a stale file can never be mistaken for
        # a newer release.
        path = destination_dir / f"{blob_sha}.bin"
        tmp = path.with_suffix(".part")
        tmp.write_bytes(blob_bytes)
        tmp.chmod(0o755)
        tmp.replace(path)
        return FetchedBlob(path=path, sha256=blob_sha, size=len(blob_bytes), media_type=str(layer.get("mediaType") or ""))
    raise last_error or OciBlobError(f"could not fetch {reference}")


def with_pull_host(reference: str, pull_host: str | None) -> str:
    """Replace the registry host of a reference; the digest is untouched."""

    if not pull_host:
        return reference
    rest = reference.split("/", 1)[1] if "/" in reference else reference
    return f"{pull_host}/{rest}"
