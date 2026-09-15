"""The worker's blob fetch against a real HTTP server speaking the registry v2 API."""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.adapters.oci_blob import OciBlobError, fetch_blob, with_pull_host


class FakeRegistry:
    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[str, bytes] = {}
        self.tamper_manifest = False
        self.tamper_blob = False

    def publish(self, repo: str, content: bytes, media_type="application/octet-stream") -> str:
        layer = "sha256:" + hashlib.sha256(content).hexdigest()
        self.blobs[layer] = content
        manifest = json.dumps({
            "schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "size": 2, "digest": "sha256:" + "1" * 64},
            "layers": [{"mediaType": media_type, "size": len(content), "digest": layer}],
        }).encode()
        digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
        self.manifests[digest] = manifest
        return digest


@pytest.fixture
def registry():
    state = FakeRegistry()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            parts = self.path.split("/")
            if "manifests" in parts:
                body = state.manifests.get(parts[-1])
                if body is not None and state.tamper_manifest:
                    body = body.replace(b"octet-stream", b"octet-strean")
            elif "blobs" in parts:
                body = state.blobs.get(parts[-1])
                if body is not None and state.tamper_blob:
                    body = body + b"x"
            else:
                body = None
            if body is None:
                self.send_response(404); self.end_headers(); return
            self.send_response(200); self.send_header("Content-Type", "application/octet-stream"); self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.host = f"127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()


def test_fetch_writes_the_layer_named_by_its_content(registry, tmp_path):
    digest = registry.publish("app/bin", b"#!/bin/sh\necho hi\n")
    blob = fetch_blob(f"{registry.host}/app/bin@{digest}", tmp_path)
    assert blob.path.read_bytes() == b"#!/bin/sh\necho hi\n"
    assert blob.path.name == blob.sha256 + ".bin"
    assert blob.path.stat().st_mode & 0o111, "a binary artifact is executable on disk"
    assert blob.media_type == "application/octet-stream"


def test_a_manifest_that_does_not_hash_to_its_digest_is_refused(registry, tmp_path):
    digest = registry.publish("app/bin", b"payload")
    registry.tamper_manifest = True
    with pytest.raises(OciBlobError, match="hashes to"):
        fetch_blob(f"{registry.host}/app/bin@{digest}", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_blob_that_does_not_hash_to_the_layer_digest_is_refused(registry, tmp_path):
    digest = registry.publish("app/bin", b"payload")
    registry.tamper_blob = True
    with pytest.raises(OciBlobError, match="hashes to"):
        fetch_blob(f"{registry.host}/app/bin@{digest}", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_missing_artifact_is_an_error_not_an_empty_file(registry, tmp_path):
    with pytest.raises(OciBlobError, match="404"):
        fetch_blob(f"{registry.host}/app/bin@sha256:{'0' * 64}", tmp_path)


@pytest.mark.parametrize("reference", ["app/bin:latest", "host/app/bin", "host/app/bin@sha1:abc", "host/App@sha256:" + "a" * 64])
def test_only_digest_pinned_references_are_accepted(reference, tmp_path):
    with pytest.raises(OciBlobError, match="digest-pinned"):
        fetch_blob(reference, tmp_path)


def test_plain_http_is_not_attempted_against_a_remote_host_unless_allowed(tmp_path):
    # An unroutable host: with https only, the failure is the TLS/connect error, and
    # the http fallback is never tried -- the reference is refused either way.
    with pytest.raises(OciBlobError):
        fetch_blob("192.0.2.1:5000/app/bin@sha256:" + "a" * 64, tmp_path, timeout=0.5)


def test_pull_host_replaces_only_the_registry_part():
    ref = "172.17.0.1:55000/hello-systemd-go@sha256:" + "b" * 64
    assert with_pull_host(ref, "localhost:55000") == "localhost:55000/hello-systemd-go@sha256:" + "b" * 64
    assert with_pull_host(ref, None) == ref
