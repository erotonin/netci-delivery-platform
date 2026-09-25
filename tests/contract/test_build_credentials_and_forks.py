"""What a build authenticates with, and what a fork's build may never do (ADR-054).

Before this no credential was bound anywhere: checkout, the cache's git mirror, buildah,
syft, trivy and cosign all ran anonymously, which a company's private git server and
registry refuse. Registry TLS and the transparency log were the scripts' insecure
defaults whatever netCI demanded. And a fork's verify-only build (ADR-043) pushed its
unsigned image to the registry, because sbom.sh and scan.sh pushed before they looked.

The Groovy cannot run here (no Jenkins), so it is held to what it must and must not
contain. The shell scripts run for real, against stub buildah/syft/trivy/cosign on PATH.
"""

from __future__ import annotations

import json
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VARS = ROOT / "jenkins" / "shared-library" / "vars"
PIPELINE = (VARS / "netciPipeline.groovy").read_text(encoding="utf-8")
REGISTRY_AUTH = (VARS / "netciRegistryAuth.groovy").read_text(encoding="utf-8")
CONTAINER_CI = ROOT / "templates" / "container-ci-cd-v1" / "scripts" / "ci"
KUBERNETES_CI = ROOT / "templates" / "kubernetes-ci-cd-v1" / "scripts" / "ci"
SYSTEMD_CI = ROOT / "templates" / "systemd-ansible-ci-cd-v1" / "scripts" / "ci"

#: Variables that hold a secret once bound.
SECRETS = ("NETCI_REGISTRY_PASSWORD", "NETCI_COSIGN_PASSWORD", "NETCI_COSIGN_PRIVATE_KEY",
           "GIT_PASSWORD", "NETCI_PIPELINE_API_KEY", "NETCI_CALLBACK_TOKEN")


# ------------------------------------------------------------------------- Groovy


def _string_literals(source: str) -> list[tuple[str, str]]:
    """(delimiter, body) of every string literal, skipping comments and slashy regexes."""

    literals: list[tuple[str, str]] = []
    i, n = 0, len(source)
    while i < n:
        if source.startswith("//", i):
            i = source.find("\n", i)
            i = n if i < 0 else i
        elif source.startswith("/*", i):
            i = source.index("*/", i) + 2
        elif source.startswith("==~ /", i):
            i = source.index("/", i + 5) + 1
            while source[i - 2] == "\\":
                i = source.index("/", i) + 1
        elif source[i] in "'\"":
            delimiter = source[i] * 3 if source.startswith(source[i] * 3, i) else source[i]
            j = i + len(delimiter)
            while not source.startswith(delimiter, j):
                j += 2 if source[j] == "\\" else 1
            literals.append((delimiter, source[i + len(delimiter):j]))
            i = j + len(delimiter)
        else:
            i += 1
    return literals


def test_no_secret_is_ever_interpolated_by_groovy():
    # A GString puts the value into the script text Jenkins writes to disk and may log;
    # a single-quoted string leaves it for the shell to read from the environment.
    offenders = []
    for path in VARS.glob("*.groovy"):
        for delimiter, body in _string_literals(path.read_text(encoding="utf-8")):
            if '"' in delimiter and any(secret in body for secret in SECRETS):
                offenders.append(f"{path.name}: {body.strip()[:80]}")
    assert not offenders, offenders


def test_no_step_echoes_a_secret_or_puts_one_in_a_url():
    for path in VARS.glob("*.groovy"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\becho\b", line):
                assert not any(secret in line for secret in SECRETS), f"{path.name}:{number}"
            assert not re.search(r"://[^\s'\"]*\$\{?[A-Z_]*(PASSWORD|TOKEN|USERNAME)", line), f"{path.name}:{number}"


def test_the_tokenizer_sees_the_scripts_it_checks():
    literals = _string_literals(PIPELINE)
    assert any("'''" == d and "NETCI_COSIGN_PRIVATE_KEY" in b for d, b in literals)
    assert any('"""' == d and "clone --mirror" in b for d, b in literals)


def test_git_credentials_are_bound_only_when_configured():
    assert "gitCredentialsId = config.get('gitCredentialsId', '')" in PIPELINE
    # GitSCM checkout, through the plugin's own askpass.
    assert re.search(r"if \(gitCredentialsId\) \{\s*//[^\n]*\n\s*remote\.credentialsId = gitCredentialsId", PIPELINE)
    # The cache's mirror fetch, through the git plugin's binding -- never a URL with a password.
    assert re.search(r"if \(gitCredentialsId\) \{\s*withCredentials\(\[gitUsernamePassword\(credentialsId: gitCredentialsId",
                     PIPELINE)
    assert re.search(r"\} else \{\s*refreshMirror\(\)", PIPELINE)
    assert "credential.helper=" in PIPELINE


def test_the_registry_credential_is_bound_only_when_configured_and_never_for_a_fork():
    assert re.search(r"registryCredentialsId = netciVerifyOnly\(\) \? '' :", PIPELINE)
    # The helper: nothing bound without an id, and a refusal if a verify-only build gets one.
    assert re.search(r"if \(!credentialsId\?\.trim\(\)\) \{\s*body\(\)\s*return", REGISTRY_AUTH)
    assert re.search(r"NETCI_PUBLISH\?\.trim\(\) == 'false'\) \{\s*error\(", REGISTRY_AUTH)
    assert "usernamePassword(credentialsId: credentialsId" in REGISTRY_AUTH
    assert "--password-stdin" in REGISTRY_AUTH and "umask 077" in REGISTRY_AUTH
    assert re.search(r"finally \{\s*sh\(label: 'registry logout', script: 'rm -rf", REGISTRY_AUTH)
    assert 'REGISTRY_AUTH_FILE=${authDir}/config.json' in REGISTRY_AUTH and 'DOCKER_CONFIG=${authDir}' in REGISTRY_AUTH


def test_only_the_stages_that_talk_to_the_registry_bind_its_credential():
    lines = PIPELINE.splitlines()
    for script in ("build.sh", "sbom.sh", "scan.sh", "publish.sh"):
        line = next(line for line in lines if f'/{script}"' in line)
        assert "netciRegistryAuth(registryCredentialsId)" in line, script
    assert "netciRegistryAuth" not in next(line for line in lines if '/test.sh"' in line)
    sign = PIPELINE[PIPELINE.index("stage('Sign')"):PIPELINE.index("stage('Custom: after sign')")]
    assert "netciRegistryAuth(registryCredentialsId)" in sign
    assert "netciRegistryAuth" not in PIPELINE[PIPELINE.index("stage('Checkout')"):PIPELINE.index("stage('Unit Test')")]


def test_the_cosign_password_is_bound_only_when_configured_and_only_in_sign():
    assert 'COSIGN_PASSWORD=""' not in PIPELINE
    assert re.search(r"if \(cosignPasswordCredentialsId\) \{\s*signingCredentials << "
                     r"string\(credentialsId: cosignPasswordCredentialsId, variable: 'NETCI_COSIGN_PASSWORD'\)", PIPELINE)
    assert PIPELINE.count("NETCI_COSIGN_PASSWORD") == 2  # the binding, and the one sign.sh call
    assert 'COSIGN_PASSWORD="${NETCI_COSIGN_PASSWORD:-}"' in PIPELINE


def test_the_signing_key_never_lands_where_it_is_archived():
    sign = PIPELINE[PIPELINE.index("stage('Sign')"):PIPELINE.index("stage('Custom: after sign')")]
    assert "NETCI_OUTPUT_DIR}/cosign.key" not in sign
    assert "trap 'rm -rf \"${key_dir}\"' EXIT" in sign
    assert "excludes: '*.oci.tar,**/*.key'" in PIPELINE


def test_tls_and_tlog_reach_the_scripts_from_the_build_parameters():
    assert 'REGISTRY_TLS_VERIFY = "${params.REGISTRY_TLS_VERIFY ?: \'\'}"' in PIPELINE
    assert 'COSIGN_TLOG_UPLOAD = "${params.COSIGN_TLOG_UPLOAD ?: \'\'}"' in PIPELINE


# ------------------------------------------------------------------------- shell

STUB = r"""#!/usr/bin/env bash
# Records every call and writes what the real tool would, so the scripts run to the end.
tool="$(basename "$0")"
printf '%s %s\n' "${tool}" "$*" >> "${STUB_LOG}"
case "${tool}" in
  buildah)
    previous=""
    for arg in "$@"; do
      if [[ "${previous}" == "--digestfile" ]]; then printf 'sha256:%064d\n' 7 > "${arg}"; fi
      previous="${arg}"
    done ;;
  syft)
    for arg in "$@"; do [[ "${arg}" == cyclonedx-json=* ]] && echo '{}' > "${arg#cyclonedx-json=}"; done ;;
  trivy)
    previous=""
    for arg in "$@"; do
      [[ "${previous}" == "--output" ]] && echo '{}' > "${arg}"
      if [[ "${previous}" == "--input" && -f "${arg}/index.json" ]]; then echo "trivy-saw-layout ${arg}" >> "${STUB_LOG}"; fi
      previous="${arg}"
    done ;;
  cosign)
    case "$1" in
      public-key) echo "PUBLIC KEY" ;;
      verify) echo '[]' ;;
      upload) echo "reg.example:5000/app@sha256:$(printf '%064d' 9)" ;;
    esac ;;
esac
exit 0
"""


@pytest.fixture()
def run(tmp_path):
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for tool in ("buildah", "syft", "trivy", "cosign"):
        (stubs / tool).write_text(STUB)
        (stubs / tool).chmod(0o755)
    out = tmp_path / "out"
    out.mkdir()
    archive = out / "app.oci.tar"
    layout = tmp_path / "layout"
    layout.mkdir()
    (layout / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": []}))
    (layout / "oci-layout").write_text('{"imageLayoutVersion": "1.0.0"}')
    with tarfile.open(archive, "w") as tar:
        for name in ("index.json", "oci-layout"):
            tar.add(layout / name, arcname=name)
    log = tmp_path / "calls.log"
    scratch = tmp_path / "workspace@tmp"  # what Jenkins calls WORKSPACE_TMP
    temporary = tmp_path / "tmp"
    temporary.mkdir()

    def _run(script: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], str]:
        log.write_text("")
        environment = {
            "PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(tmp_path), "STUB_LOG": str(log),
            "NETCI_OUTPUT_DIR": str(out), "NETCI_APP_DIR": str(tmp_path), "NETCI_IMAGE_NAME": "app",
            "IMAGE_TAG": "abc123", "REGISTRY_PUSH_HOST": "reg.example:5000", "WORKSPACE_TMP": str(scratch),
            "TMPDIR": str(temporary), "ARTIFACT_PATH": str(archive), "COSIGN_ATTEST_PROVENANCE": "false",
        }
        environment.update(env)
        result = subprocess.run(["bash", str(script)], env=environment, capture_output=True, text=True, timeout=60)
        return result, log.read_text()

    _run.out = out  # type: ignore[attr-defined]
    _run.archive = archive  # type: ignore[attr-defined]
    _run.temporary = temporary  # type: ignore[attr-defined]
    return _run


def _pushes(calls: str) -> list[str]:
    return [line for line in calls.splitlines() if line.startswith("buildah push") and "docker://" in line]


@pytest.mark.parametrize("ci_dir", [CONTAINER_CI, KUBERNETES_CI], ids=["container", "kubernetes"])
def test_a_verify_only_sbom_describes_the_local_archive_and_pushes_nothing(run, ci_dir):
    result, calls = run(ci_dir / "sbom.sh", NETCI_PUBLISH="false")
    assert result.returncode == 0, result.stderr
    assert not _pushes(calls)
    assert f"syft oci-archive:{run.archive} " in calls
    assert not (run.out / "registry-artifact-ref.txt").exists()
    assert (run.out / "sbom.json").is_file()


@pytest.mark.parametrize("ci_dir", [CONTAINER_CI, KUBERNETES_CI], ids=["container", "kubernetes"])
def test_a_verify_only_scan_reads_the_unpacked_archive_and_pushes_nothing(run, ci_dir):
    result, calls = run(ci_dir / "scan.sh", NETCI_PUBLISH="false")
    assert result.returncode == 0, result.stderr
    assert not _pushes(calls)
    assert "trivy-saw-layout" in calls
    assert "--input" in calls and "reg.example" not in next(line for line in calls.splitlines() if line.startswith("trivy"))
    # Unpacked outside the archived evidence, at a path trivy does not read as a reference
    # (WORKSPACE_TMP is `<job>@tmp`), and gone afterwards.
    trivy = next(line for line in calls.splitlines() if line.startswith("trivy image"))
    layout = trivy.split("--input ", 1)[1].split()[0]
    assert layout.startswith(str(run.temporary)) and "@" not in layout
    assert not list(run.temporary.iterdir())
    assert not [p for p in run.out.iterdir() if p.is_dir()]


def test_a_published_build_still_pushes_then_scans_the_pushed_digest(run):
    result, calls = run(CONTAINER_CI / "sbom.sh", NETCI_PUBLISH="true", REGISTRY_TLS_VERIFY="true")
    assert result.returncode == 0, result.stderr
    assert len(_pushes(calls)) == 1 and "--tls-verify=true" in _pushes(calls)[0]
    digest = "sha256:" + "0" * 63 + "7"
    assert f"syft registry:reg.example:5000/app@{digest} " in calls
    result, calls = run(CONTAINER_CI / "scan.sh", NETCI_PUBLISH="true", REGISTRY_TLS_VERIFY="true")
    assert result.returncode == 0, result.stderr
    assert not _pushes(calls), "push-image.sh is idempotent: the digest is already recorded"
    trivy = next(line for line in calls.splitlines() if line.startswith("trivy"))
    assert trivy.endswith(f"reg.example:5000/app@{digest}") and "--insecure" not in trivy


def test_a_build_from_an_older_netci_with_no_publish_parameter_still_publishes(run):
    result, calls = run(CONTAINER_CI / "sbom.sh")
    assert result.returncode == 0, result.stderr
    assert len(_pushes(calls)) == 1


def test_push_image_refuses_a_verify_only_build_whoever_calls_it(run):
    result, calls = run(CONTAINER_CI / "push-image.sh", NETCI_PUBLISH="false")
    assert result.returncode != 0
    assert "verify-only" in result.stderr
    assert not _pushes(calls)


def test_the_systemd_template_uploads_nothing_for_a_verify_only_build(run):
    result, calls = run(SYSTEMD_CI / "sign.sh", NETCI_PUBLISH="false", COSIGN_KEY_REF="/dev/null")
    assert result.returncode != 0 and "verify-only" in result.stderr
    assert "cosign upload" not in calls
    # Nothing else in that template talks to the registry at all.
    for script in ("build.sh", "test.sh", "sbom.sh", "scan.sh", "publish.sh"):
        code = "\n".join(line for line in (SYSTEMD_CI / script).read_text(encoding="utf-8").splitlines()
                         if not line.lstrip().startswith("#"))
        assert not re.search(r"\bcosign\b|buildah push|docker://|oras push|curl", code), script


def _cosign_sign_call(calls: str) -> str:
    return next(line for line in calls.splitlines() if line.startswith("cosign sign --yes "))


def test_cosign_verifies_tls_and_uploads_to_the_tlog_when_netci_says_so(run):
    result, calls = run(CONTAINER_CI / "sign.sh", NETCI_PUBLISH="true", COSIGN_KEY_REF="/dev/null",
                        REGISTRY_TLS_VERIFY="true", COSIGN_TLOG_UPLOAD="true",
                        COSIGN_REKOR_URL="https://rekor.corp.example")
    assert result.returncode == 0, result.stderr
    sign = _cosign_sign_call(calls)
    assert "--allow-insecure-registry" not in sign and "--tlog-upload=false" not in sign
    assert "--rekor-url https://rekor.corp.example" in sign
    verify = next(line for line in calls.splitlines() if line.startswith("cosign verify "))
    assert "--insecure-ignore-tlog" not in verify and "--allow-insecure-registry" not in verify
    assert "--rekor-url https://rekor.corp.example" in verify


@pytest.mark.parametrize("template", ["container", "systemd"])
def test_a_tlog_upload_with_no_rekor_named_is_refused_rather_than_sent_to_the_public_log(run, template):
    script = (CONTAINER_CI if template == "container" else SYSTEMD_CI) / "sign.sh"
    result, calls = run(script, NETCI_PUBLISH="true", COSIGN_KEY_REF="/dev/null",
                        REGISTRY_TLS_VERIFY="true", COSIGN_TLOG_UPLOAD="true")
    assert result.returncode != 0 and "COSIGN_REKOR_URL" in result.stderr
    assert "cosign sign" not in calls


def test_the_rekor_url_reaches_the_scripts_from_the_build_parameters():
    assert 'COSIGN_REKOR_URL = "${params.COSIGN_REKOR_URL ?: \'\'}"' in PIPELINE


def test_the_lab_settings_keep_the_lab_behaviour(run):
    # netCI with registry.allowHttp and no tlog: exactly what every build did before.
    for env in ({"REGISTRY_TLS_VERIFY": "false", "COSIGN_TLOG_UPLOAD": "false"}, {}):
        result, calls = run(CONTAINER_CI / "sign.sh", NETCI_PUBLISH="true", COSIGN_KEY_REF="/dev/null", **env)
        assert result.returncode == 0, result.stderr
        sign = _cosign_sign_call(calls)
        assert "--allow-insecure-registry" in sign and "--tlog-upload=false" in sign


@pytest.mark.parametrize("ci_dir", [CONTAINER_CI, SYSTEMD_CI], ids=["container", "systemd"])
def test_cosign_is_never_insecure_against_a_registry_whose_tls_is_verified(run, ci_dir):
    result, _ = run(ci_dir / "sign.sh", NETCI_PUBLISH="true", COSIGN_KEY_REF="/dev/null",
                    REGISTRY_TLS_VERIFY="true", COSIGN_ALLOW_INSECURE_REGISTRY="true")
    assert result.returncode != 0
    assert "contradicts" in result.stderr


@pytest.mark.parametrize("name", ["REGISTRY_TLS_VERIFY", "COSIGN_TLOG_UPLOAD", "NETCI_PUBLISH"])
def test_a_malformed_switch_is_refused_rather_than_read_as_one_or_the_other(run, name):
    result, _ = run(CONTAINER_CI / "sbom.sh", **{name: "yes"})
    assert result.returncode != 0
    assert f"{name} must be true or false" in result.stderr
