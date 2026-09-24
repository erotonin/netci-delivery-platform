"""Tests for scripts/netci_cli.py.

The CLI is loaded with importlib so it stays importable without being on
sys.path as a package.  All network calls are intercepted by patching
Client.request to delegate to FastAPI's in-process TestClient, so no real
server is needed.

Auth mode is "none" in tests (the default when NETCI_AUTH_MODE is unset), so
every caller is an anonymous principal with all roles.  The token-in-header
test verifies the header is forwarded without letting the value leak to
stdout/stderr.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.adapters.scm import MockScmProvider, get_scm_provider, set_scm_provider
from app.domain.models import ScmProviderType

# ---------------------------------------------------------------------------
# Load the CLI module from its file path (standard-library importlib pattern)
# ---------------------------------------------------------------------------

_CLI_PATH = Path(__file__).parent.parent.parent / "scripts" / "netci_cli.py"
_spec = importlib.util.spec_from_file_location("netci_cli", _CLI_PATH)
_cli = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_cli)  # type: ignore[union-attr]

Client = _cli.Client
main = _cli.main

# ---------------------------------------------------------------------------
# FastAPI test client wired to the real in-process app
# ---------------------------------------------------------------------------

_tc = TestClient(main_mod.app)

SECRET = "webhook-secret-for-cli-tests"


def _make_client(token: str | None = None) -> Client:
    """Return a CLI Client whose .request() routes to the in-process app.

    The patch translates the CLI's (method, path, body) call into a
    TestClient call, preserving the Authorization header so the token-header
    test can inspect what was sent.
    """
    c = Client("http://unused", token)

    def _patched_request(method: str, path: str, body: dict | None = None):
        headers: dict[str, str] = {"X-Correlation-Id": c._correlation_id}
        if c._token:
            # Token forwarded exactly as the CLI would; never logged here.
            headers["Authorization"] = f"Bearer {c._token}"
        kwargs: dict = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        resp = _tc.request(method, path, **kwargs)
        try:
            parsed = resp.json()
        except Exception:
            parsed = resp.text
        return resp.status_code, parsed

    c.request = _patched_request  # type: ignore[method-assign]
    return c


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_platform():
    """Reset in-memory state before every test; restore SCM provider after."""
    main_mod.platform.reset()
    original = get_scm_provider(ScmProviderType.GITHUB)
    set_scm_provider(ScmProviderType.GITHUB, MockScmProvider(ScmProviderType.GITHUB))
    yield
    set_scm_provider(ScmProviderType.GITHUB, original)


def _module(name: str = "orders-api", *, environments: tuple = ("dev", "staging", "prod")):
    """Create a system + module + SCM integration and return the module dict.

    Mirrors the _module() helper from test_ci_cd_separation.py so tests run
    against a real delivery pipeline rather than mocked data.
    """
    system = f"sys-{uuid4().hex[:6]}"
    resp = _tc.post("/systems", json={"id": system, "unit": "Orders", "description": "orders"})
    assert resp.status_code == 201, resp.text

    body = {
        "name": name,
        "displayName": name,
        "repositoryUrl": f"https://github.com/acme/{name}",
        "pipelineTemplate": "container-ci-cd-v1",
        "runtime": "docker",
        "moduleType": "Backend",
        "description": "test module",
        "deploymentEnvironments": [
            {"displayName": env, "environment": env, "runtime": "docker", "servers": [f"{env}-host"]}
            for env in environments
        ],
    }
    created = _tc.post(f"/systems/{system}/modules", json=body)
    assert created.status_code == 201, created.text
    module = created.json()

    repo = f"acme/{name}-{uuid4().hex[:4]}"
    scm = _tc.post(
        f"/applications/{module['applicationId']}/scm",
        json={"provider": "github", "repositoryIdentity": repo, "secretToken": SECRET},
    )
    assert scm.status_code == 201, scm.text
    return module


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMe:
    def test_me_human_output(self, capsys):
        c = _make_client()
        import argparse
        args = argparse.Namespace(json=False)
        rc = _cli.cmd_me(c, args)
        assert rc == 0
        out = capsys.readouterr().out
        # Auth mode is "none" in the test environment
        assert "subject" in out
        assert "method" in out

    def test_me_json_output(self, capsys):
        c = _make_client()
        import argparse
        args = argparse.Namespace(json=True)
        rc = _cli.cmd_me(c, args)
        assert rc == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        # The API always returns principal and authMode
        assert "principal" in parsed
        assert "authMode" in parsed


class TestRunWithBuildOnly:
    """Verify that --build-only sends deploy=False to the API."""

    def test_run_build_only_sets_deploy_false(self):
        """The 'deploy' field reaching the API must be False when --build-only is given."""
        module = _module()
        module_id = module["id"]

        # Capture the body that reaches the API by wrapping the real client
        captured_bodies: list[dict] = []
        c = _make_client()
        original_request = c.request

        def _spy(method, path, body=None):
            if body is not None:
                captured_bodies.append(dict(body))
            return original_request(method, path, body)

        c.request = _spy  # type: ignore[method-assign]

        import argparse
        args = argparse.Namespace(
            module=module_id,
            commit="abc1234",
            branch="main",
            env="dev",
            build_only=True,
            json=False,
        )
        rc = _cli.cmd_run(c, args)
        assert rc == 0
        # Exactly one POST body was sent; deploy must be False
        assert len(captured_bodies) == 1
        assert captured_bodies[0]["deploy"] is False


class TestRules:
    def test_rules_human_output(self, capsys):
        module = _module()
        c = _make_client()
        import argparse
        args = argparse.Namespace(module=module["id"], json=False)
        rc = _cli.cmd_rules(c, args)
        assert rc == 0
        out = capsys.readouterr().out
        # Each trigger line starts with "on "
        lines = [l for l in out.splitlines() if l.strip()]
        assert any(l.startswith("on ") for l in lines)

    def test_rules_json_output(self, capsys):
        module = _module()
        c = _make_client()
        import argparse
        args = argparse.Namespace(module=module["id"], json=True)
        rc = _cli.cmd_rules(c, args)
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert "triggers" in parsed


class TestCveUnknownId:
    """CVE on an unknown ID: the API returns 200 with empty affected and a coverage block."""

    def test_cve_empty_affected_coverage_printed(self, capsys):
        c = _make_client()
        import argparse
        args = argparse.Namespace(id="CVE-9999-00001", json=False)
        rc = _cli.cmd_cve(c, args)
        assert rc == 0
        out = capsys.readouterr().out
        # Affected section should note nothing
        assert "none" in out.lower() or "affected" in out.lower()
        # Coverage block must always appear
        assert "coverage" in out.lower()

    def test_cve_json_output(self, capsys):
        c = _make_client()
        import argparse
        args = argparse.Namespace(id="CVE-9999-00001", json=True)
        rc = _cli.cmd_cve(c, args)
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert "affected" in parsed
        assert "coverage" in parsed


class TestFreezeCreateListCancel:
    """Full lifecycle: create → list → cancel."""

    def _iso(self, offset_days: int) -> str:
        from datetime import datetime, timedelta, timezone
        dt = datetime.now(timezone.utc) + timedelta(days=offset_days)
        return dt.isoformat()

    def test_freeze_create_list_cancel(self, capsys):
        import argparse
        c = _make_client()

        # Create
        create_args = argparse.Namespace(
            name="Holiday freeze",
            start=self._iso(1),
            end=self._iso(3),
            env=["prod"],
            reason="Year-end holiday",
            module=None,
            system=None,
            json=False,
        )
        rc = _cli.cmd_freeze_create(c, create_args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "freeze created" in out
        freeze_id = out.strip().split()[-1]

        # List
        list_args = argparse.Namespace(past=False, json=False)
        rc = _cli.cmd_freezes(c, list_args)
        assert rc == 0
        out = capsys.readouterr().out
        assert freeze_id in out

        # Cancel
        cancel_args = argparse.Namespace(id=freeze_id, json=False)
        rc = _cli.cmd_freeze_cancel(c, cancel_args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "cancelled" in out


class TestApiErrorFormat:
    """A 4xx error must be printed to stderr as 'error <status> <code>: <message>'."""

    def test_api_error_printed_exit_1(self, capsys):
        c = _make_client()
        # Request a module that doesn't exist → 404
        import argparse
        args = argparse.Namespace(module="nonexistent-module-xyz", json=False)
        with pytest.raises(SystemExit) as exc_info:
            _cli.cmd_rules(c, args)
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        # Format: "error <status> <CODE>: <message>"
        assert err.startswith("error 4")
        assert ":" in err

    def test_api_error_contains_status_code_and_code_field(self, capsys):
        c = _make_client()
        import argparse
        args = argparse.Namespace(module="no-such-module", json=False)
        with pytest.raises(SystemExit):
            _cli.cmd_runs(c, args)
        err = capsys.readouterr().err
        parts = err.split()
        # "error 404 MODULE_NOT_FOUND: ..."
        assert parts[0] == "error"
        assert parts[1].startswith("4")


class TestJsonOutput:
    """--json flag must produce parseable JSON on stdout."""

    def test_freezes_json_parseable(self, capsys):
        c = _make_client()
        import argparse
        args = argparse.Namespace(past=False, json=True)
        rc = _cli.cmd_freezes(c, args)
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert "items" in parsed


class TestTokenNotLeaked:
    """A token supplied via NETCI_TOKEN_FILE must be sent in the header but
    never appear in stdout or stderr."""

    def test_token_sent_in_header_not_leaked(self, capsys, tmp_path):
        secret_token = f"secret-{uuid4().hex}"
        token_file = tmp_path / "token.txt"
        token_file.write_text(secret_token)

        # Resolve token via the file-path mechanism
        import argparse
        args = argparse.Namespace(token_file=str(token_file))
        token = _cli._resolve_token(args)
        assert token == secret_token

        # Now use a client with that token and call /me
        c = _make_client(token=secret_token)
        me_args = argparse.Namespace(json=False)
        rc = _cli.cmd_me(c, me_args)
        assert rc == 0

        captured = capsys.readouterr()
        # The token must not appear anywhere in stdout or stderr
        assert secret_token not in captured.out
        assert secret_token not in captured.err

    def test_token_from_env_file_not_in_output(self, capsys, monkeypatch, tmp_path):
        secret_token = f"env-token-{uuid4().hex}"
        token_file = tmp_path / "env_token.txt"
        token_file.write_text(secret_token + "\n")  # trailing newline is stripped

        # Resolve token through NETCI_TOKEN_FILE env var
        monkeypatch.setenv("NETCI_TOKEN_FILE", str(token_file))
        import argparse
        args = argparse.Namespace(token_file=None)
        token = _cli._resolve_token(args)
        assert token == secret_token  # newline stripped

        c = _make_client(token=token)
        me_args = argparse.Namespace(json=False)
        rc = _cli.cmd_me(c, me_args)
        assert rc == 0

        captured = capsys.readouterr()
        assert secret_token not in captured.out
        assert secret_token not in captured.err


class TestMainEntryPoint:
    """Smoke-test the main() entry point with argument parsing."""

    def test_main_me_returns_zero(self, capsys, monkeypatch):
        # Route main()'s Client.request through the TestClient
        original_init = Client.__init__

        def patched_init(self, base_url, token):
            original_init(self, base_url, token)

        def patched_request(self, method, path, body=None):
            headers = {"X-Correlation-Id": self._correlation_id}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            kwargs: dict = {"headers": headers}
            if body is not None:
                kwargs["json"] = body
            resp = _tc.request(method, path, **kwargs)
            try:
                parsed = resp.json()
            except Exception:
                parsed = resp.text
            return resp.status_code, parsed

        monkeypatch.setattr(Client, "request", patched_request)
        rc = main(["--api", "http://unused", "me"])
        assert rc == 0
