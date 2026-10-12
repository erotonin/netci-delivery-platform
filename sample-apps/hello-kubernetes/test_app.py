from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from app import Handler


class ApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_health_reports_version_and_environment(self) -> None:
        with patch.dict(os.environ, {"APP_VERSION": "sha256:test", "NETCI_ENVIRONMENT": "staging"}):
            with urllib.request.urlopen(f"{self.base_url}/healthz", timeout=2) as response:
                body = json.load(response)

        self.assertEqual(200, response.status)
        self.assertEqual("ok", body["status"])
        self.assertEqual("sha256:test", body["version"])
        self.assertEqual("staging", body["environment"])

    def test_unknown_path_is_not_found(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"{self.base_url}/missing", timeout=2)
        self.assertEqual(404, error.exception.code)


if __name__ == "__main__":
    unittest.main()
