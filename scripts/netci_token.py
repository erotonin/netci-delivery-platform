#!/usr/bin/env python3
"""Issue, list and revoke netCI access tokens for `NETCI_AUTH_MODE=token`.

The token file stores SHA-256 hashes, never the tokens themselves, so this is the only
moment the credential exists in readable form: it is printed once and cannot be recovered
afterwards. Losing it means issuing a new one, which is the intended trade -- a file that
could hand back working credentials is a file whose every backup is a liability.

    python scripts/netci_token.py issue  --subject dana --name "Dana Developer" \\
                                         --email dana@corp.example --role developer
    python scripts/netci_token.py list
    python scripts/netci_token.py revoke --subject dana

The file to edit comes from --file or $NETCI_AUTH_TOKENS_FILE. netCI re-reads it when its
mtime changes, so issuing and revoking take effect without restarting the API.

For an identity provider (Entra ID, Keycloak, Okta, Google Workspace), use
NETCI_AUTH_MODE=oidc instead and map IdP groups to netCI roles -- there is then no token
file to manage, and leavers lose access when the directory says so. See
docs/security-model.md.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sys
from pathlib import Path

import yaml


ROLES = ("viewer", "developer", "reviewer", "platform-admin")
ROLE_HELP = {
    "viewer": "read everything; change nothing",
    "developer": "create applications and run dev/staging pipelines",
    "reviewer": "approve production, and run production pipelines",
    "platform-admin": "everything a reviewer can do, plus platform administration",
}


def resolve_path(argument: str | None) -> Path:
    path = argument or os.getenv("NETCI_AUTH_TOKENS_FILE", "")
    if not path:
        raise SystemExit(
            "set --file or NETCI_AUTH_TOKENS_FILE to the token file netCI is configured with"
        )
    return Path(path)


def load(path: Path) -> dict:
    if not path.exists():
        return {"principals": []}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("principals", []), list):
        raise SystemExit(f"{path} must be a mapping with a 'principals' list")
    payload.setdefault("principals", [])
    return payload


def save(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# netCI access tokens for NETCI_AUTH_MODE=token.\n"
        "#\n"
        "# Only SHA-256 hashes are stored: this file cannot give a token back, and a leaked\n"
        "# copy grants nothing. Manage it with scripts/netci_token.py, and treat it as\n"
        "# configuration rather than a secret store -- for real identity, prefer\n"
        "# NETCI_AUTH_MODE=oidc so joiners and leavers follow the corporate directory.\n"
        "#\n"
        "# netCI re-reads this file when its mtime changes; no restart is needed.\n"
    )
    body = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    path.write_text(header + body, encoding="utf-8")
    # The file names who may approve a production release. Other users on the host have
    # no reason to read it.
    try:
        path.chmod(0o600)
    except OSError:
        pass


def issue(arguments: argparse.Namespace) -> int:
    path = resolve_path(arguments.file)
    document = load(path)
    if any(entry.get("subject") == arguments.subject for entry in document["principals"]):
        raise SystemExit(
            f"{arguments.subject} already has a token; revoke it first if you are rotating it"
        )

    token = f"netci_{secrets.token_urlsafe(32)}"
    document["principals"].append(
        {
            "subject": arguments.subject,
            "displayName": arguments.name or arguments.subject,
            "email": arguments.email or "",
            "roles": list(arguments.role),
            "tokenSha256": hashlib.sha256(token.encode()).hexdigest(),
        }
    )
    save(path, document)

    print(f"issued a token for {arguments.subject} ({', '.join(arguments.role)}) in {path}\n")
    print(token)
    print("\nGive this to the user now -- it is not stored and cannot be shown again.")
    if "platform-admin" in arguments.role:
        print(
            "\nNote: platform-admin can approve production. Prefer separate reviewer and\n"
            "developer tokens for day-to-day work so separation of duties still applies."
        )
    return 0


def list_tokens(arguments: argparse.Namespace) -> int:
    path = resolve_path(arguments.file)
    document = load(path)
    if not document["principals"]:
        print(f"no tokens in {path}")
        return 0
    print(f"{'SUBJECT':<24} {'ROLES':<34} DISPLAY NAME")
    for entry in document["principals"]:
        roles = ", ".join(entry.get("roles", []))
        print(f"{entry.get('subject', ''):<24} {roles:<34} {entry.get('displayName', '')}")
    return 0


def revoke(arguments: argparse.Namespace) -> int:
    path = resolve_path(arguments.file)
    document = load(path)
    remaining = [entry for entry in document["principals"] if entry.get("subject") != arguments.subject]
    if len(remaining) == len(document["principals"]):
        raise SystemExit(f"no token for {arguments.subject} in {path}")
    document["principals"] = remaining
    save(path, document)
    print(f"revoked {arguments.subject}; netCI will refuse that token on its next request")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--file", help="token file (default: $NETCI_AUTH_TOKENS_FILE)")
    commands = parser.add_subparsers(dest="command", required=True)

    issue_parser = commands.add_parser(
        "issue",
        help="issue a token and print it once",
        description="Roles:\n" + "\n".join(f"  {name:<16}{help}" for name, help in ROLE_HELP.items()),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    issue_parser.add_argument("--subject", required=True, help="stable id recorded in the audit trail")
    issue_parser.add_argument("--name", help="display name shown in the Portal")
    issue_parser.add_argument("--email", help="contact address")
    issue_parser.add_argument(
        "--role", action="append", choices=ROLES, required=True, help="repeatable"
    )
    issue_parser.set_defaults(handler=issue)

    list_parser = commands.add_parser("list", help="show who holds a token, and with which roles")
    list_parser.set_defaults(handler=list_tokens)

    revoke_parser = commands.add_parser("revoke", help="remove a subject's token")
    revoke_parser.add_argument("--subject", required=True)
    revoke_parser.set_defaults(handler=revoke)

    arguments = parser.parse_args()
    return arguments.handler(arguments)


if __name__ == "__main__":
    sys.exit(main())
