from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path not in {"/", "/healthz"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        version = os.getenv("APP_VERSION", "dev")
        environment = os.getenv("NETCI_ENVIRONMENT", "dev")
        body = {
            "service": "hello-kubernetes",
            "status": "ok",
            "version": version,
            "environment": environment,
        }
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


def serve() -> None:
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    serve()
