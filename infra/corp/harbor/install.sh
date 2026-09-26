#!/usr/bin/env bash
# Harbor for the company-shaped lab (ADR-056): the registry builds push to and clusters pull
# from. Its own Trivy scanner is not installed -- netCI's policy rests on the build's scan.
set -euo pipefail

VERSION="v2.15.2"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SECRETS="${ROOT}/.netci-gate/corp"
WORK="${SECRETS}/harbor-installer"          # outside the repository tree: never committed
DATA="${HARBOR_DATA_DIR:-/home/${USER}/netci-corp-harbor-data}"
mkdir -p "${WORK}" "${DATA}"
for name in harbor_admin_password harbor_db_password; do
  [[ -s "${SECRETS}/${name}" ]] || ( umask 077; python3 -c 'import secrets;print(secrets.token_urlsafe(24))' > "${SECRETS}/${name}" )
done
if [[ ! -x "${WORK}/harbor/install.sh" ]]; then
  curl -fsSL "https://github.com/goharbor/harbor/releases/download/${VERSION}/harbor-online-installer-${VERSION}.tgz" \
    | tar -xz -C "${WORK}"
fi
# Start from the installer's own harbor.yml.tmpl, so every key this Harbor version expects is
# there, and override only what the lab needs. Rendered by python, not sed: a password may
# hold any character sed treats specially.
python3 - "${WORK}/harbor/harbor.yml.tmpl" "${WORK}/harbor/harbor.yml" "${SECRETS}" "${DATA}" <<'PY'
import os, sys, yaml
template, target, secrets, data = sys.argv[1:]
config = yaml.safe_load(open(template))
config["hostname"] = "172.17.0.1"
config["http"] = {"port": 8930}
config.pop("https", None)                       # plain HTTP inside the lab; TLS is a company install
config["harbor_admin_password"] = open(f"{secrets}/harbor_admin_password").read().strip()
config["database"]["password"] = open(f"{secrets}/harbor_db_password").read().strip()
config["database"]["max_idle_conns"] = 20
config["database"]["max_open_conns"] = 50
config["data_volume"] = data
config["jobservice"]["max_job_workers"] = 2
config["log"]["level"] = "warning"
old = os.umask(0o077)
yaml.safe_dump(config, open(target, "w"), sort_keys=False)
os.umask(old)
PY
cd "${WORK}/harbor"
# prepare writes secret-bearing config as root; Harbor documents running the installer as root.
sudo -n ./install.sh   # no --with-trivy: netCI owns scanning (ADR-056)
echo "harbor ${VERSION} at http://172.17.0.1:8930"
