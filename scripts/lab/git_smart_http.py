#!/usr/bin/env python3
"""Serve bare repositories over git's smart HTTP protocol, read-only.

The lab used to serve repositories with `python -m http.server` (the "dumb" protocol),
which made every fetch walk the object store file by file: ~6 s per fetch of a
repository that was already up to date, in every build, on both the mirror refresh and
the checkout. That is a property of the lab server, not of netCI or its cache, and it
would have been mistaken for one in the benchmark. A real deployment has GitLab/Gitea;
this is the smallest thing that speaks the same protocol: `git http-backend` behind a
plain CGI bridge.

    python3 git_smart_http.py --root /srv/git --port 80
"""

from __future__ import annotations

import argparse
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class GitHandler(BaseHTTPRequestHandler):
    root = "/srv/git"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler's signature
        pass

    def _serve(self) -> None:
        url = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        env = {
            **{k: v for k, v in os.environ.items() if k in {"PATH", "HOME"}},
            "GIT_PROJECT_ROOT": self.root,
            "GIT_HTTP_EXPORT_ALL": "1",
            # The repositories are bind-mounted from the developer's uid; the server
            # runs as another. Read-only serving of a mount we chose is not the attack
            # git's ownership check guards against.
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
            "REQUEST_METHOD": self.command,
            "PATH_INFO": url.path,
            "QUERY_STRING": url.query,
            "CONTENT_TYPE": self.headers.get("Content-Type") or "",
            "CONTENT_LENGTH": str(length),
            "REMOTE_ADDR": self.client_address[0],
            "SERVER_PROTOCOL": self.request_version,
            "GATEWAY_INTERFACE": "CGI/1.1",
        }
        encoding = self.headers.get("Content-Encoding")
        if encoding:
            env["HTTP_CONTENT_ENCODING"] = encoding
        completed = subprocess.run(["git", "http-backend"], input=body, capture_output=True, env=env, check=False)
        raw = completed.stdout
        head, _, payload = raw.partition(b"\r\n\r\n")
        status = 200
        headers: list[tuple[str, str]] = []
        for line in head.decode(errors="replace").splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key.strip().lower() == "status":
                status = int(value.strip().split()[0])
            else:
                headers.append((key.strip(), value.strip()))
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _serve
    do_POST = _serve


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/srv/git")
    parser.add_argument("--port", type=int, default=80)
    args = parser.parse_args()
    GitHandler.root = args.root
    server = ThreadingHTTPServer(("0.0.0.0", args.port), GitHandler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
