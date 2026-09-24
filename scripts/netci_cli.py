#!/usr/bin/env python3
"""netCI command-line client.

Talks to the netCI Delivery API over HTTP.  Standard library only -- no
third-party dependencies.  Every request carries a stable correlation id
so an operator can join CLI activity to server logs by id prefix.

Security rules that are never relaxed:
- The bearer token is read from a file or an environment variable; it is
  never echoed to stdout/stderr, never placed on an argv, and never written
  to a log line.
- Subprocesses are never launched with secrets anywhere on their argv.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from uuid import uuid4


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class Client:
    """Minimal HTTP wrapper around the netCI API.

    All requests attach Authorization (when a token is present) and a
    per-invocation X-Correlation-Id so log lines can be correlated without
    exposing a secret.
    """

    def __init__(self, base_url: str, token: str | None) -> None:
        self.base_url = base_url.rstrip("/")
        # Token is stored but never printed; keep it out of __repr__ too.
        self._token = token
        # One correlation id for the entire CLI invocation.
        self._correlation_id = f"cli-{uuid4()}"

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
    ) -> tuple[int, object]:
        """Send *method* to *path*, return (status, parsed_body).

        On network failure raises urllib.error.URLError so callers can
        distinguish that from an HTTP error response.
        """
        headers: dict[str, str] = {
            "Accept": "application/json",
            "X-Correlation-Id": self._correlation_id,
        }
        # Attach the bearer token when we have one; never log it.
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        raw_body: bytes | None = None
        if body is not None:
            raw_body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=raw_body,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read().decode(errors="replace")
                return resp.status, (json.loads(payload) if payload else None)
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode(errors="replace")
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                # Wrap plain-text error bodies so callers always see a dict.
                parsed = {"code": "HTTP_ERROR", "message": payload or str(exc)}
            return exc.code, parsed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _die(status: int, body: object) -> int:
    """Print API error to stderr and return exit code 1.

    Format: "error <status> <code>: <message>"
    The token must never appear in this output.
    """
    if isinstance(body, dict):
        code = body.get("code", "HTTP_ERROR")
        message = body.get("message", str(body))
    else:
        code = "HTTP_ERROR"
        message = str(body)
    print(f"error {status} {code}: {message}", file=sys.stderr)
    return 1


def _check(status: int, body: object, *, ok: tuple[int, ...] = (200,)) -> object:
    """Return body when status is acceptable, else call sys.exit(1).

    Raises SystemExit so the caller never has to check the return code.
    """
    if status not in ok:
        sys.exit(_die(status, body))
    return body


def _print_table(rows: list[list[str]], headers: list[str]) -> None:
    """Print a left-aligned fixed-width table to stdout."""
    all_rows = [headers] + rows
    widths = [max(len(r[i]) for r in all_rows) for i in range(len(headers))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt.format(*row))


def _trunc(value: str | None, n: int) -> str:
    """Return at most *n* characters of *value*, empty string for None."""
    if value is None:
        return ""
    s = str(value)
    return s[:n] if len(s) > n else s


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------


def cmd_me(client: Client, args: argparse.Namespace) -> int:
    status, body = client.request("GET", "/me")
    _check(status, body)
    if args.json:
        print(json.dumps(body, indent=2))
        return 0
    p = body.get("principal", {})  # type: ignore[union-attr]
    print(f"subject:  {p.get('subject', '-')}")
    print(f"method:   {body.get('authMode', '-')}")  # type: ignore[union-attr]
    print(f"roles:    {', '.join(p.get('roles', [])) or '-'}")
    return 0


def cmd_modules(client: Client, args: argparse.Namespace) -> int:
    """List systems or drill into a single system's modules."""
    if args.system:
        status, body = client.request("GET", f"/systems/{args.system}")
        _check(status, body)
        modules = body.get("modules", [])  # type: ignore[union-attr]
        if args.json:
            print(json.dumps(body, indent=2))
            return 0
        rows = [[m.get("id", ""), m.get("name", ""), m.get("runtime", "")] for m in modules]
        _print_table(rows, ["id", "name", "runtime"])
    else:
        status, body = client.request("GET", "/systems")
        _check(status, body)
        systems: list = body if isinstance(body, list) else []  # type: ignore[assignment]
        if args.json:
            print(json.dumps(systems, indent=2))
            return 0
        rows = []
        for sys_item in systems:
            for m in sys_item.get("modules", []):
                rows.append([m.get("id", ""), m.get("name", ""), m.get("runtime", "")])
        _print_table(rows, ["id", "name", "runtime"])
    return 0


def cmd_runs(client: Client, args: argparse.Namespace) -> int:
    """List recent pipeline runs for a module."""
    path = f"/modules/{args.module}/pipeline-runs"
    status, body = client.request("GET", path)
    _check(status, body)
    items = body.get("items", body) if isinstance(body, dict) else body  # type: ignore[union-attr]
    if not isinstance(items, list):
        items = []
    if args.limit:
        items = items[: args.limit]
    if args.json:
        print(json.dumps(items, indent=2))
        return 0
    rows = [
        [
            _trunc(r.get("id"), 8),
            r.get("status", ""),
            r.get("branch", ""),
            # trigger is a dict; we show the event key
            _trunc((r.get("trigger") or {}).get("event"), 16),
            str(r.get("deployAfterBuild", "")),
            _trunc(r.get("artifactDigest"), 19),
            _trunc(r.get("createdAt"), 24),
        ]
        for r in items
    ]
    _print_table(rows, ["id", "status", "branch", "trigger.event", "deployAfterBuild", "artifactDigest", "createdAt"])
    return 0


def cmd_run(client: Client, args: argparse.Namespace) -> int:
    """Start a new pipeline run."""
    body: dict = {
        "commitSha": args.commit,
        "environment": args.env,
        # deploy is the inverse of --build-only
        "deploy": not args.build_only,
    }
    if args.branch:
        body["branch"] = args.branch
    status, resp = client.request("POST", f"/modules/{args.module}/pipeline-runs", body)
    _check(status, resp, ok=(202,))
    if args.json:
        print(json.dumps(resp, indent=2))
        return 0
    run_id = resp.get("id", "?") if isinstance(resp, dict) else "?"  # type: ignore[union-attr]
    print(f"queued run {run_id}")
    return 0


def cmd_watch(client: Client, args: argparse.Namespace) -> int:
    """Poll a run until it reaches a terminal state."""
    terminal = {"succeeded", "failed", "cancelled", "rolled_back"}
    last_status = None
    while True:
        status, body = client.request("GET", f"/pipeline-runs/{args.run_id}")
        _check(status, body)
        current = body.get("status", "") if isinstance(body, dict) else ""  # type: ignore[union-attr]
        if current != last_status:
            print(f"status: {current}")
            last_status = current
        if current in terminal:
            if args.json:
                print(json.dumps(body, indent=2))
            # Only succeeded is exit 0; all other terminal states are failure.
            return 0 if current == "succeeded" else 1
        time.sleep(args.interval)


def cmd_logs(client: Client, args: argparse.Namespace) -> int:
    """Print the collected log lines for a pipeline run."""
    status, body = client.request("GET", f"/pipeline-runs/{args.run_id}/logs")
    _check(status, body)
    if args.json:
        print(json.dumps(body, indent=2))
        return 0
    lines: list = body.get("lines", []) if isinstance(body, dict) else []  # type: ignore[union-attr]
    if args.tail:
        lines = lines[-args.tail :]
    for line in lines:
        print(line)
    return 0


def cmd_promote(client: Client, args: argparse.Namespace) -> int:
    """Promote an existing artifact to dev or staging without rebuilding."""
    body = {"pipelineRunId": args.run_id, "environment": args.env}
    status, resp = client.request("POST", f"/modules/{args.module}/promotions", body)
    _check(status, resp, ok=(202,))
    if args.json:
        print(json.dumps(resp, indent=2))
        return 0
    print("promotion accepted")
    return 0


def cmd_rules(client: Client, args: argparse.Namespace) -> int:
    """Print the delivery rules for a module."""
    status, body = client.request("GET", f"/modules/{args.module}/delivery-rules")
    _check(status, body)
    if args.json:
        print(json.dumps(body, indent=2))
        return 0
    for trigger in body.get("triggers", []) if isinstance(body, dict) else []:  # type: ignore[union-attr]
        on = trigger.get("on", "?")
        patterns = trigger.get("branches") or trigger.get("tags") or ["*"]
        pat_str = ",".join(patterns)
        deploy_to = trigger.get("deployTo")
        action = f"deployTo={deploy_to}" if deploy_to else "build only"
        if trigger.get("registerVersion"):
            action += " + register version"
        print(f"on {on} {pat_str} -> {action}")
    if isinstance(body, dict):
        print(f"fork pull requests: {body.get('forkPullRequests')}")
        for env, rule in (body.get("promotion") or {}).items():
            need = rule.get("requireHealthyIn")
            print(f"promote to {env}: " + (f"needs healthy in {need} for {rule.get('minSoakMinutes', 0)} min" if need else "no prior environment required"))
        print("prod: only through a production request")
    return 0


def cmd_versions(client: Client, args: argparse.Namespace) -> int:
    """List published versions for a module."""
    status, body = client.request("GET", f"/modules/{args.module}/versions")
    _check(status, body)
    if args.json:
        print(json.dumps(body, indent=2))
        return 0
    items = body.get("items", body) if isinstance(body, dict) else body  # type: ignore[union-attr]
    if not isinstance(items, list):
        items = []
    for item in items:
        print(item if isinstance(item, str) else json.dumps(item))
    return 0


def cmd_cve(client: Client, args: argparse.Namespace) -> int:
    """Show exposure for a specific vulnerability id across all modules."""
    status, body = client.request("GET", f"/vulnerabilities/{args.id}/exposure")
    _check(status, body)
    if args.json:
        print(json.dumps(body, indent=2))
        return 0

    # --- affected modules ---
    affected: list = body.get("affected", []) if isinstance(body, dict) else []  # type: ignore[union-attr]
    if affected:
        rows = [
            [
                a.get("moduleId", ""),
                a.get("environment", ""),
                a.get("severity", ""),
                a.get("package", ""),
                a.get("installedVersion", ""),
                a.get("fixedVersion", ""),
            ]
            for a in affected
        ]
        _print_table(rows, ["module", "env", "severity", "package", "installed", "fixed"])
    else:
        print("affected: (none)")

    # --- coverage block, always printed ---
    coverage: dict = body.get("coverage", {}) if isinstance(body, dict) else {}  # type: ignore[union-attr]
    print(
        f"\ncoverage: inService={coverage.get('inService', 0)}"
        f"  withSbom={coverage.get('withSbom', 0)}"
        f"  rescanned={coverage.get('rescanned', 0)}"
        f"  rescanFailed={coverage.get('rescanFailed', 0)}"
        f"  oldest={coverage.get('oldestRescanAt', 'n/a')}"
    )
    not_covered: list = coverage.get("notCovered", [])
    if not_covered:
        print("notCovered:")
        for nc in not_covered:
            print(f"  module={nc.get('moduleId','')} env={nc.get('environment','')} reason={nc.get('reason','')}")
    return 0


def cmd_freezes(client: Client, args: argparse.Namespace) -> int:
    """List change-freeze windows."""
    path = "/change-freezes"
    if args.past:
        path += "?includePast=true"
    status, body = client.request("GET", path)
    _check(status, body)
    if args.json:
        print(json.dumps(body, indent=2))
        return 0
    items: list = body.get("items", []) if isinstance(body, dict) else []  # type: ignore[union-attr]
    if not items:
        print("(no change freezes)")
        return 0
    rows = [
        [
            _trunc(f.get("id"), 36),
            f.get("name", ""),
            ",".join(f.get("environments", [])),
            _trunc(f.get("startsAt"), 24),
            _trunc(f.get("endsAt"), 24),
        ]
        for f in items
    ]
    _print_table(rows, ["id", "name", "environments", "startsAt", "endsAt"])
    return 0


def cmd_freeze_create(client: Client, args: argparse.Namespace) -> int:
    """Create a new change-freeze window."""
    body: dict = {
        "name": args.name,
        "startsAt": args.start,
        "endsAt": args.end,
        "environments": args.env,
        "reason": args.reason,
    }
    if args.module:
        body["moduleId"] = args.module
    if args.system:
        body["systemId"] = args.system
    status, resp = client.request("POST", "/change-freezes", body)
    _check(status, resp, ok=(201,))
    if args.json:
        print(json.dumps(resp, indent=2))
        return 0
    print(f"freeze created: {resp.get('id', '?') if isinstance(resp, dict) else '?'}")
    return 0


def cmd_freeze_cancel(client: Client, args: argparse.Namespace) -> int:
    """Cancel an active change-freeze window."""
    status, resp = client.request("POST", f"/change-freezes/{args.id}/cancel")
    _check(status, resp)
    if args.json:
        print(json.dumps(resp, indent=2))
        return 0
    print(f"freeze cancelled: {args.id}")
    return 0


# ---------------------------------------------------------------------------
# Token resolution  (never printed, never on argv of subprocesses)
# ---------------------------------------------------------------------------


def _resolve_token(args: argparse.Namespace) -> str | None:
    """Return the bearer token or None.

    Priority:
    1. --token-file <path>
    2. The file named by NETCI_TOKEN_FILE
    3. NETCI_TOKEN env var
    """
    token_file: str | None = getattr(args, "token_file", None)
    if token_file is None:
        token_file = os.environ.get("NETCI_TOKEN_FILE")
    if token_file:
        try:
            with open(token_file) as fh:
                # Strip whitespace so a trailing newline does not break the header.
                return fh.read().strip()
        except OSError as exc:
            # The file path may contain a secret-ish name; only report the
            # errno, not the path, to avoid hinting at the layout.
            print(f"error: cannot read token file ({exc.strerror})", file=sys.stderr)
            sys.exit(1)
    return os.environ.get("NETCI_TOKEN")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="netci",
        description="netCI command-line client",
    )
    p.add_argument(
        "--api",
        metavar="URL",
        default=os.environ.get("NETCI_API_URL", "http://127.0.0.1:8000"),
        help="Base URL of the netCI API (default: NETCI_API_URL or http://127.0.0.1:8000)",
    )
    p.add_argument(
        "--token-file",
        metavar="PATH",
        default=None,
        help="File containing the bearer token (overrides NETCI_TOKEN_FILE / NETCI_TOKEN)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Print raw JSON response instead of a human-readable table",
    )

    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # me
    sub.add_parser("me", help="Show current identity")

    # modules
    m_mod = sub.add_parser("modules", help="List modules")
    m_mod.add_argument("--system", metavar="S", default=None, help="Filter to a specific system id")

    # runs
    m_runs = sub.add_parser("runs", help="List pipeline runs for a module")
    m_runs.add_argument("module", help="Module id")
    m_runs.add_argument("--limit", metavar="N", type=int, default=None)

    # run
    m_run = sub.add_parser("run", help="Start a pipeline run")
    m_run.add_argument("module", help="Module id")
    m_run.add_argument("--commit", required=True, metavar="SHA", help="Commit SHA to build")
    m_run.add_argument("--branch", default=None, metavar="B")
    m_run.add_argument("--env", default="dev", choices=["dev", "staging", "prod"], metavar="dev|staging|prod")
    m_run.add_argument("--build-only", action="store_true", default=False,
                       help="Build without deploying (sets deploy=false)")

    # watch
    m_watch = sub.add_parser("watch", help="Poll a run until it finishes")
    m_watch.add_argument("run_id", metavar="RUN_ID")
    m_watch.add_argument("--interval", type=float, default=10.0, metavar="SEC")

    # logs
    m_logs = sub.add_parser("logs", help="Fetch log lines for a run")
    m_logs.add_argument("run_id", metavar="RUN_ID")
    m_logs.add_argument("--tail", type=int, default=None, metavar="N")

    # promote
    m_prom = sub.add_parser("promote", help="Promote an artifact to dev or staging")
    m_prom.add_argument("module", help="Module id")
    m_prom.add_argument("run_id", metavar="RUN_ID")
    m_prom.add_argument("--env", required=True, choices=["dev", "staging"], metavar="dev|staging")

    # rules
    m_rules = sub.add_parser("rules", help="Show delivery rules for a module")
    m_rules.add_argument("module", help="Module id")

    # versions
    m_ver = sub.add_parser("versions", help="List published versions for a module")
    m_ver.add_argument("module", help="Module id")

    # cve
    m_cve = sub.add_parser("cve", help="Show vulnerability exposure across modules")
    m_cve.add_argument("id", metavar="ID", help="Vulnerability id, e.g. CVE-2024-1234")

    # freezes
    m_fz = sub.add_parser("freezes", help="List change-freeze windows")
    m_fz.add_argument("--past", action="store_true", default=False, help="Include past freezes")

    # freeze (sub-commands: create, cancel)
    m_freeze = sub.add_parser("freeze", help="Manage a single change-freeze window")
    freeze_sub = m_freeze.add_subparsers(dest="freeze_cmd", metavar="create|cancel")
    freeze_sub.required = True

    m_fc = freeze_sub.add_parser("create", help="Create a change-freeze window")
    m_fc.add_argument("--name", required=True, metavar="N")
    m_fc.add_argument("--start", required=True, metavar="ISO", dest="start")
    m_fc.add_argument("--end", required=True, metavar="ISO", dest="end")
    m_fc.add_argument("--env", required=True, action="append", dest="env",
                      choices=["dev", "staging", "prod"], metavar="dev|staging|prod")
    m_fc.add_argument("--reason", required=True, metavar="R")
    m_fc.add_argument("--module", default=None, metavar="M")
    m_fc.add_argument("--system", default=None, metavar="S")

    m_fcancel = freeze_sub.add_parser("cancel", help="Cancel an active change-freeze window")
    m_fcancel.add_argument("id", metavar="ID")

    return p


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_DISPATCH: dict[str, object] = {}  # filled after function definitions


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    token = _resolve_token(args)
    base_url = args.api
    client = Client(base_url, token)

    command = args.command
    if command == "me":
        return cmd_me(client, args)
    if command == "modules":
        return cmd_modules(client, args)
    if command == "runs":
        return cmd_runs(client, args)
    if command == "run":
        return cmd_run(client, args)
    if command == "watch":
        return cmd_watch(client, args)
    if command == "logs":
        return cmd_logs(client, args)
    if command == "promote":
        return cmd_promote(client, args)
    if command == "rules":
        return cmd_rules(client, args)
    if command == "versions":
        return cmd_versions(client, args)
    if command == "cve":
        return cmd_cve(client, args)
    if command == "freezes":
        return cmd_freezes(client, args)
    if command == "freeze":
        freeze_cmd = args.freeze_cmd
        if freeze_cmd == "create":
            return cmd_freeze_create(client, args)
        if freeze_cmd == "cancel":
            return cmd_freeze_cancel(client, args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
