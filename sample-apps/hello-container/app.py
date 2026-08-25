import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

VERSION = os.getenv("APP_VERSION", "dev")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/healthz"):
            self.send_response(404)
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"service": "hello-container", "status": "ok", "version": VERSION}).encode())

    def log_message(self, format, *args):
        return


HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
