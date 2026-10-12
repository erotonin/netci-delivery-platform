"""Hygiene tests for corporate shell scripts under scripts/corp/ and infra/corp/.

Corporate shell scripts provision clusters, manage secrets, and perform automated
failovers. These contract tests ensure every script adheres to strict execution safety,
prevents credentials from leaking into process tables or command lines, disallows force
pushes that would alter shared library git history, and ensures secret files are written
with restrictive file permissions (umask 077).
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

# Repository root resolved relative to this test file (tests/contract/ -> root)
REPO_ROOT = Path(__file__).resolve().parents[2]

# Discover all shell scripts in corporate script and infrastructure directories
CORP_SCRIPTS = sorted(
    set(
        list((REPO_ROOT / "scripts/corp").glob("**/*.sh"))
        + list((REPO_ROOT / "infra/corp").glob("**/*.sh"))
    )
)

# ---------------------------------------------------------------------------
# Rule 2 regex patterns and detection logic
# ---------------------------------------------------------------------------

# Flag curl invocations where -u or --user contains $(cat ...) command substitution.
# Credentials in command line arguments are exposed to all local users via `ps aux`.
CURL_USER_CAT_PATTERN = re.compile(
    r'\bcurl\b[^|;&\n]*?\s(?:-u=?|--user(?:=|\s+))\s*["\']?[^"\'\s|;&]*?\$\(\s*cat\b',
    re.IGNORECASE,
)

# Flag -H or --header with PRIVATE-TOKEN: containing $(cat ...).
# GitLab API tokens must be passed on stdin via curl -K -, never in header arguments.
PRIVATE_TOKEN_CAT_PATTERN = re.compile(
    r'-(?:H|-header)\s*["\']?PRIVATE-TOKEN:\s*[^"\'\n]*?\$\(\s*cat\b',
    re.IGNORECASE,
)

# Flag -H or --header with Authorization: containing $(cat ...).
# Bearer tokens or Basic auth must not be interpolated into command line flags.
AUTHORIZATION_CAT_PATTERN = re.compile(
    r'-(?:H|-header)\s*["\']?Authorization:\s*[^"\'\n]*?\$\(\s*cat\b',
    re.IGNORECASE,
)

# Flag docker login with -p or --password. Only --password-stdin is permitted
# so that the registry password is never exposed in the process table.
DOCKER_LOGIN_PASSWORD_PATTERN = re.compile(
    r'\bdocker\s+login\b[^|;&\n]*?(?:(?<=\s)-p\b|(?<=\s)-p=|(?<=\s)--password(?!-stdin\b))',
)

# Match curl, docker, git, or helm when invoked as the command in a pipeline segment or subshell.
# This accounts for environment variable prefixes (e.g. GIT_ASKPASS=...), wrappers (sudo, exec),
# command substitutions $(cmd ...), and subshells.
CMD_INVOCATION_PATTERN = re.compile(
    r'(?:^|[;&|()=`]|\$\(|\b(?:sudo(?:\s+-\S+)*|exec|nohup|time|builtin|command)\s+|\bif\s+|\bwhile\s+|\buntil\s+|!\s+)'
    r'(?:[A-Za-z_][A-Za-z0-9_]*=(?:[^\s"\']+|"[^"]*"|\'[^\']*\')\s+)*'
    r'(curl|docker|git|helm)\b'
)

# Flag $(cat ...) command substitutions where the target path/file contains
# password, token, secret, or key.
CAT_SECRET_FILE_PATTERN = re.compile(
    r'\$\(\s*cat\b[^)]*?(?:password|token|secret|key)',
    re.IGNORECASE,
)

# Match shell redirection ('>' or '>>') writing to a path under ${SECRETS... or ${C}/...
# Example: > "${SECRETS}/gitlab_admin_token" or > "${C}/$1"
WRITE_SECRET_REDIRECT_PATTERN = re.compile(
    r'>\s*["\']?\$(?:\{SECRETS|SECRETS\b|\{C\}|C\/|\{C\b)',
)


def find_secret_command_line_violation(line: str) -> str | None:
    """Inspect a single shell script line for secrets exposed on command lines.

    Returns an error description if a violation is detected, or None if the line is clean.
    Accepted safe patterns include passing secrets via stdin (e.g. `printf ... | curl -K -`
    or `docker login ... --password-stdin < file`).
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    # Check for header arguments with cat in PRIVATE-TOKEN or Authorization
    if PRIVATE_TOKEN_CAT_PATTERN.search(line):
        return "PRIVATE-TOKEN header contains $(cat ...)"
    if AUTHORIZATION_CAT_PATTERN.search(line):
        return "Authorization header contains $(cat ...)"

    # Check for docker login using -p or --password instead of --password-stdin
    if DOCKER_LOGIN_PASSWORD_PATTERN.search(line):
        return "docker login uses -p or --password instead of --password-stdin"

    # Split line into pipeline stages and chained commands (| and ||, &&, ;)
    # This ensures arguments belong to the actual command, not a preceding printf in a pipeline.
    segments = re.split(r'\|&?|&&|\|\||;', line)
    for segment in segments:
        # Check curl with -u or --user containing $(cat ...)
        if re.search(r'\bcurl\b', segment) and CURL_USER_CAT_PATTERN.search(segment):
            return "curl command contains -u/--user with $(cat ...)"

        # Check for $(cat <secret>) inside an argument of curl, docker, git, or helm
        if CMD_INVOCATION_PATTERN.search(segment):
            if CAT_SECRET_FILE_PATTERN.search(segment):
                return "argument of curl/docker/git/helm contains $(cat ...) with secret filename"

    return None


# ---------------------------------------------------------------------------
# Test functions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script_path", CORP_SCRIPTS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_script_shebang_and_strict_mode(script_path: Path) -> None:
    """Ensure scripts start with standard bash shebang and enable strict error handling.

    Scripts in corporate infrastructure manage production clusters, failovers, and secrets.
    Uncaught errors or unbound variables could leave a failover half-applied or delete data.
    status.sh is the single intentional exception: it probes health read-only and must continue
    even when a probed service is temporarily unreachable (set -uo pipefail without -e).
    """
    rel = script_path.relative_to(REPO_ROOT)
    lines = script_path.read_text(encoding="utf-8").splitlines()
    assert lines, f"{rel}:1: script is empty"

    # Line 1 must be the standard portable bash shebang
    first_line = lines[0].strip()
    assert first_line == "#!/usr/bin/env bash", (
        f"{rel}:1: expected shebang '#!/usr/bin/env bash', got '{first_line}'"
    )

    # status.sh is read-only and intentionally omits -e so failed probes don't abort it
    is_status_script = script_path.name == "status.sh"
    expected_set = "set -uo pipefail" if is_status_script else "set -euo pipefail"

    # Locate any existing 'set ' statement to report the exact line number on mismatch
    set_line_no = None
    for idx, line in enumerate(lines, start=1):
        if line.strip().startswith("set "):
            set_line_no = idx
            break

    line_no = set_line_no if set_line_no is not None else 1
    content = "\n".join(lines)
    assert expected_set in content, (
        f"{rel}:{line_no}: script must contain '{expected_set}'"
    )
    if not is_status_script:
        assert "set -uo pipefail" not in content or "set -euo pipefail" in content, (
            f"{rel}:{line_no}: only status.sh is permitted to omit -e in set flags"
        )


@pytest.mark.parametrize("script_path", CORP_SCRIPTS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_secrets_on_command_line(script_path: Path) -> None:
    """Ensure scripts never pass secret values directly on command lines.

    Command line arguments are readable by unprivileged local users via /proc or `ps`.
    Secrets must be passed via stdin (e.g. `printf ... | curl -K -` or `--password-stdin`).
    """
    rel = script_path.relative_to(REPO_ROOT)
    lines = script_path.read_text(encoding="utf-8").splitlines()
    violations: list[str] = []

    for line_no, line in enumerate(lines, start=1):
        reason = find_secret_command_line_violation(line)
        if reason:
            violations.append(f"{rel}:{line_no}: secret on command line: {reason}\n    {line.strip()}")

    assert not violations, "\n".join(violations)


@pytest.mark.parametrize("script_path", CORP_SCRIPTS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_git_force_push(script_path: Path) -> None:
    """Ensure no script force pushes to a git repository.

    Force pushing alters upstream history, unpins tags, and destroys auditability.
    Shared libraries and application branches must only be updated through normal,
    fast-forward commits or merge requests.
    """
    rel = script_path.relative_to(REPO_ROOT)
    lines = script_path.read_text(encoding="utf-8").splitlines()
    violations: list[str] = []

    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Check if the line invokes git push and contains -f or --force
        if re.search(r'\bgit\s+push\b', line):
            if " -f" in line or "--force" in line:
                violations.append(
                    f"{rel}:{line_no}: forbidden force push in git push: {stripped}"
                )

    assert not violations, "\n".join(violations)


@pytest.mark.parametrize("script_path", CORP_SCRIPTS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_secrets_written_with_umask(script_path: Path) -> None:
    """Ensure secret files written to disk are created with restrictive permissions.

    If a script writes to ${SECRETS} or ${C}/ using output redirection (>), it must also
    set umask 077 so secret files are not readable by other users on the host.
    """
    rel = script_path.relative_to(REPO_ROOT)
    lines = script_path.read_text(encoding="utf-8").splitlines()
    content = "\n".join(lines)
    has_umask = "umask 077" in content

    violations: list[str] = []
    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if WRITE_SECRET_REDIRECT_PATTERN.search(line):
            if not has_umask:
                violations.append(
                    f"{rel}:{line_no}: secret file written with '>' without 'umask 077' in script: {stripped}"
                )

    assert not violations, "\n".join(violations)


def test_secret_on_command_line_detector_not_vacuous() -> None:
    """Verify the Rule 2 detector identifies prohibited patterns and permits accepted forms.

    Checks bad samples (curl -u with cat, PRIVATE-TOKEN/Authorization headers with cat,
    docker login with -p/--password, and curl/docker/git/helm with $(cat <secret>)) to ensure
    the detector flags them, while verifying the approved `printf ... | curl -K -` pattern
    and `--password-stdin` forms pass without false positives.
    """
    bad_samples = [
        # Explicit example from requirements: curl with -u and $(cat ...)
        'curl -fsS -u "admin:$(cat "$S/jenkins_admin_password")" x',
        # curl with -u and $(cat ...) unquoted
        'curl -u admin:$(cat "$SECRETS/admin_password") https://jenkins.corp.local',
        # PRIVATE-TOKEN header with $(cat ...)
        'curl -fsS -H "PRIVATE-TOKEN: $(cat "$SECRETS/gitlab_admin_token")" https://gitlab.corp.local/api',
        # Authorization header with $(cat ...)
        'curl -H "Authorization: Bearer $(cat "$SECRETS/token")" https://api.corp.local',
        # docker login with -p
        'docker login "${REGISTRY}" -u admin -p $(cat password.txt)',
        # docker login with --password
        'docker login "${REGISTRY}" -u admin --password secret_password',
        # git command with $(cat ...) referencing a token
        'git clone https://$(cat "$SECRETS/gitlab_token")@gitlab.corp.local/repo.git',
        # helm command with $(cat ...) referencing a secret
        'helm upgrade --install app ./chart --set secret=$(cat "$SECRETS/app_secret")',
    ]

    for bad_line in bad_samples:
        violation = find_secret_command_line_violation(bad_line)
        assert violation is not None, f"Expected bad sample to be flagged: {bad_line}"

    accepted_samples = [
        # Accepted Jenkins API call from scripts/corp/jenkins_failover.sh
        'printf \'user = "admin:%s"\\n\' "$(<"${SECRETS}/jenkins_admin_password")" | curl -K - -fsS "$@"',
        # Accepted GitLab API call from infra/corp/gitlab/bootstrap.sh
        'printf \'header = "PRIVATE-TOKEN: %s"\\n\' "$(<"${SECRETS}/gitlab_admin_token")" | curl -K - -fsS -H \'Content-Type: application/json\' "$@"',
        # Accepted generic curl -K - form with printf
        'printf \'%s\' "$(cat "${SECRETS}/token")" | curl -K - -fsS https://api.corp.local',
        # Accepted docker login with --password-stdin from scripts/corp/up.sh
        'docker login "${HARBOR_PUSH}" -u "$(cat "${C}/harbor_robot_name")" --password-stdin < "${C}/harbor_robot_secret" >/dev/null',
        # Normal curl command without secrets
        'curl -s -o /dev/null http://172.17.0.1:8929/users/sign_in',
        # Normal git command without secrets
        'git clone -q --bare "${ROOT}/.netci-gate/git/payments-api.git" "${work}/app.git"',
    ]

    for good_line in accepted_samples:
        violation = find_secret_command_line_violation(good_line)
        assert violation is None, f"Accepted line was falsely flagged ({violation}): {good_line}"
