#!/usr/bin/env python3
"""Append every Alertmanager webhook notification to a JSON-lines file (lab receiver)."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    out = Path("alerts.jsonl")

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {"raw": body.decode(errors="replace")}
        with self.out.open("a") as handle:
            handle.write(json.dumps({"receivedAt": datetime.now(timezone.utc).isoformat(), "notification": payload}) + "\n")
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9094)
    parser.add_argument("--out", default="alerts.jsonl")
    args = parser.parse_args()
    Handler.out = Path(args.out)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
