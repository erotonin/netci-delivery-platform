#!/usr/bin/env bash
# A Redfish BMC in front of the lab's VMs (sushy-tools' emulator on libvirt), so that the
# supervisor fences through Redfish -- the protocol of real servers' BMCs -- rather than the
# lab's SSH agent.
#
#   lab/redfish.sh start    install (venv), generate TLS and credentials once, start the emulator
#   lab/redfish.sh stop
#   lab/redfish.sh secret   (re)create the supervisor's fence Secret and print the Helm values
#   lab/redfish.sh quirks start|stop   a real BMC's behaviour in front of it, on port 8001
#                                      (lab/redfish-quirks.py); then BMC_PORT=8001 for secret
#
# The emulator may act on netci-lab-1..3 only (SUSHY_EMULATOR_ALLOWED_INSTANCES, by libvirt UUID):
# the host's other VMs are not visible through it. It listens on the libvirt network's host
# address, with TLS (a self-signed certificate the supervisor pins by SHA-256) and basic auth.
# Secrets stay under .netci-gate/lab/redfish and are never printed.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="${ROOT}/.netci-gate/lab"
R="${STATE}/redfish"
export KUBECONFIG="${STATE}/kubeconfig"
LISTEN=192.168.122.1 PORT=8000
VIRSH=(virsh -c qemu:///system)

uuids() { for n in netci-lab-1 netci-lab-2 netci-lab-3; do "${VIRSH[@]}" domuuid "$n"; done | grep -v '^$'; }

setup() {
  mkdir -m 700 -p "${R}"
  [[ -x "${R}/venv/bin/sushy-emulator" ]] || {
    python3 -m venv --system-site-packages "${R}/venv"  # libvirt's Python bindings come from the system
    "${R}/venv/bin/pip" install -q sushy-tools==2.1.0 bcrypt
  }
  ( umask 077
    [[ -s "${R}/tls.key" ]] || openssl req -x509 -newkey rsa:3072 -nodes -days 825 -subj "/CN=netci-lab-bmc" \
      -addext "subjectAltName=IP:${LISTEN}" -keyout "${R}/tls.key" -out "${R}/tls.crt" 2>/dev/null
    [[ -s "${R}/password" ]] || openssl rand -hex 24 | tr -d '\n' > "${R}/password"
    "${R}/venv/bin/python" -c 'import bcrypt,sys; print("netci:" + bcrypt.hashpw(open(sys.argv[1]).read().encode(), bcrypt.gensalt()).decode())' \
      "${R}/password" > "${R}/htpasswd"
    openssl x509 -in "${R}/tls.crt" -outform DER | sha256sum | cut -d' ' -f1 | tr -d '\n' > "${R}/tls-sha256"
  )
  local allowed; allowed="$(uuids | sed "s/.*/'&'/" | paste -sd, -)"
  cat > "${R}/emulator.conf" <<CONF
SUSHY_EMULATOR_LISTEN_IP = '${LISTEN}'
SUSHY_EMULATOR_LISTEN_PORT = ${PORT}
SUSHY_EMULATOR_SSL_CERT = '${R}/tls.crt'
SUSHY_EMULATOR_SSL_KEY = '${R}/tls.key'
SUSHY_EMULATOR_AUTH_FILE = '${R}/htpasswd'
SUSHY_EMULATOR_LIBVIRT_URI = 'qemu:///system'
SUSHY_EMULATOR_ALLOWED_INSTANCES = [${allowed}]
CONF
}

case "${1:-}" in
  start)
    setup
    if [[ -s "${R}/pid" ]] && kill -0 "$(cat "${R}/pid")" 2>/dev/null; then echo "running"; exit 0; fi
    nohup "${R}/venv/bin/sushy-emulator" --config "${R}/emulator.conf" > "${R}/emulator.log" 2>&1 &
    echo $! > "${R}/pid"
    for _ in $(seq 1 30); do
      curl -s --cacert "${R}/tls.crt" -u "netci:$(cat "${R}/password")" -o /dev/null -w '%{http_code}' \
        "https://${LISTEN}:${PORT}/redfish/v1/Systems" 2>/dev/null | grep -q 200 && { echo "emulator up"; exit 0; }
      sleep 1
    done
    echo "the emulator did not answer; see ${R}/emulator.log" >&2; exit 1 ;;
  stop)
    [[ -s "${R}/pid" ]] && kill "$(cat "${R}/pid")" 2>/dev/null || true; rm -f "${R}/pid" ;;
  quirks)
    case "${2:-}" in
      start)
        if [[ -s "${R}/quirks.pid" ]] && kill -0 "$(cat "${R}/quirks.pid")" 2>/dev/null; then echo "running"; exit 0; fi
        nohup python3 "${ROOT}/lab/redfish-quirks.py" "${R}" > "${R}/quirks.log" 2>&1 &
        echo $! > "${R}/quirks.pid"
        for _ in $(seq 1 20); do
          curl -s --cacert "${R}/tls.crt" -o /dev/null -w '%{http_code}' "https://${LISTEN}:8001/redfish/v1/" 2>/dev/null | grep -qE '200|401' && { echo "quirks up on 8001"; exit 0; }
          sleep 1
        done
        echo "the quirks proxy did not answer; see ${R}/quirks.log" >&2; exit 1 ;;
      stop) [[ -s "${R}/quirks.pid" ]] && kill "$(cat "${R}/quirks.pid")" 2>/dev/null || true; rm -f "${R}/quirks.pid" ;;
      *) echo "usage: lab/redfish.sh quirks start|stop" >&2; exit 2 ;;
    esac ;;
  secret)
    python3 - "${R}" "${LISTEN}" "${BMC_PORT:-${PORT}}" "$(uuids | paste -sd' ' -)" "${REQUEST_TIMEOUT:-}" <<'PY' > "${R}/fence.json"
import json, sys
r, listen, port, uuids, timeout = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4].split(), sys.argv[5]
nodes = {f"netci-lab-{i+1}": {"redfish": {"endpoint": f"https://{listen}:{port}", "system": f"/redfish/v1/Systems/{u}",
                                          "credentials": "/etc/netci/fence/bmc", **({"requestTimeout": timeout} if timeout else {})}}
         for i, u in enumerate(uuids)}
print(json.dumps({"nodes": nodes}, indent=1))
PY
    printf 'netci' > "${R}/username"
    kubectl -n netci-system create secret generic netci-fence-redfish --from-file=fence.json="${R}/fence.json" \
      --from-file=bmc.username="${R}/username" --from-file=bmc.password="${R}/password" \
      --from-file=bmc.tls-sha256="${R}/tls-sha256" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    cat <<VALUES
supervisor:
  fence:
    configSecretName: netci-fence-redfish
    items:
      - {key: fence.json, path: fence.json}
      - {key: bmc.username, path: bmc/username}
      - {key: bmc.password, path: bmc/password}
      - {key: bmc.tls-sha256, path: bmc/tls-sha256}
VALUES
    ;;
  *) sed -n '2,12p' "$0" >&2; exit 2 ;;
esac
