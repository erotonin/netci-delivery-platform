#!/usr/bin/env python3
"""A Redfish BMC that behaves like a real one, in front of the lab's sushy-tools emulator.

sushy-tools answers at once, powers a VM off the moment it is asked, and always succeeds. Real
BMCs do not, and the supervisor's fencing has to be right with them:
  - every request takes LATENCY seconds (iDRAC and iLO answer in about 0.5-3 s);
  - the first ForceOff each machine gets is answered 503: the BMC is busy, nothing happens;
  - after a ForceOff, PowerState still says On for OFF_LAG seconds;
  - a ForceOff to a machine that is already off is answered 409 with iDRAC's message,
    "Server is already powered OFF" (Red Hat bug 1873305).

    lab/redfish.sh quirks start|stop     (listens on 192.168.122.1:8001, same TLS and credentials)

Credentials pass through to the emulator unchanged and are never logged.
"""
from __future__ import annotations

import http.server
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

LISTEN, PORT = "192.168.122.1", int(os.environ.get("QUIRKS_PORT", "8001"))
BACKEND = "https://192.168.122.1:8000"
LATENCY = float(os.environ.get("LATENCY", "1.5"))
OFF_LAG = float(os.environ.get("OFF_LAG", "8"))
STATE_DIR = sys.argv[1]

backend_tls = ssl.create_default_context(cafile=os.path.join(STATE_DIR, "tls.crt"))
lock = threading.Lock()
busy_left: dict[str, int] = {}     # system -> ForceOffs still to refuse with 503
forced_at: dict[str, float] = {}   # system -> when a ForceOff was carried out


def system_of(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[:5]) if len(parts) >= 5 else path  # /redfish/v1/Systems/<id>


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # the default logs the request line only
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def forward(self, method: str, body: bytes | None) -> tuple[int, bytes]:
        req = urllib.request.Request(BACKEND + self.path, data=body, method=method)
        for h in ("Authorization", "Content-Type", "Accept"):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])
        try:
            with urllib.request.urlopen(req, context=backend_tls, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def answer(self, status: int, body: bytes) -> None:
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        time.sleep(LATENCY)
        status, body = self.forward("GET", None)
        sysp = system_of(self.path)
        with lock:
            lagging = sysp in forced_at and time.time() - forced_at[sysp] < OFF_LAG
        if status == 200 and lagging and self.path.rstrip("/") == sysp:
            d = json.loads(body)
            if d.get("PowerState") == "Off":
                d["PowerState"] = "On"  # the BMC has not caught up with the power yet
                body = json.dumps(d).encode()
        self.answer(status, body)

    def do_POST(self) -> None:
        time.sleep(LATENCY)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        sysp = system_of(self.path)
        kind = ""
        try:
            kind = json.loads(body or b"{}").get("ResetType", "")
        except ValueError:
            pass
        if kind == "ForceOff":
            with lock:
                left = busy_left.setdefault(sysp, 1)
                if left > 0:
                    busy_left[sysp] = left - 1
                    self.log_message("ForceOff %s: answering 503 (busy)", sysp)
                    self.answer(503, b'{"error":{"message":"The BMC is busy; try again"}}')
                    return
            req = urllib.request.Request(BACKEND + sysp, method="GET")
            req.add_header("Authorization", self.headers.get("Authorization", ""))
            try:
                with urllib.request.urlopen(req, context=backend_tls, timeout=10) as r:
                    power = json.loads(r.read()).get("PowerState")
            except (urllib.error.URLError, ValueError):
                power = None
            if power == "Off":
                self.log_message("ForceOff %s: already off, answering 409", sysp)
                self.answer(409, json.dumps({"error": {"code": "Base.1.12.GeneralError", "message": "",
                            "@Message.ExtendedInfo": [{"Message": "Server is already powered OFF.",
                                                       "MessageId": "IDRAC.2.9.PSU501"}]}}).encode())
                return
        status, out = self.forward("POST", body)
        if kind == "ForceOff" and 200 <= status < 300:
            with lock:
                forced_at[sysp] = time.time()
            self.log_message("ForceOff %s: carried out; PowerState lags %.0f s", sysp, OFF_LAG)
        if kind == "On" and 200 <= status < 300:
            with lock:
                forced_at.pop(sysp, None)
                busy_left.pop(sysp, None)  # the next fencing of this machine meets a busy BMC again
        self.answer(status, out)


def main() -> None:
    srv = http.server.ThreadingHTTPServer((LISTEN, PORT), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.path.join(STATE_DIR, "tls.crt"), os.path.join(STATE_DIR, "tls.key"))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    sys.stderr.write(f"quirks on {LISTEN}:{PORT}: latency {LATENCY}s, PowerState lag {OFF_LAG}s, first ForceOff 503\n")
    srv.serve_forever()


if __name__ == "__main__":
    main()
