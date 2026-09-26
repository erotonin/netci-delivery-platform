#!/usr/bin/env bash
# Private certificate authority for the corp lab and server certificates it signs,
# so lab services (S3 first, Harbor later) can serve TLS that clients verify -- never
# "insecure-skip-verify".
#
# Usage:
#   scripts/corp/lab_ca.sh ca                       # create the CA once (idempotent)
#   scripts/corp/lab_ca.sh issue <name> <san>...    # issue/renew a server cert, e.g.
#       scripts/corp/lab_ca.sh issue s3 IP:172.17.0.1 DNS:netci-corp-s3
#
# Keys are generated under umask 077 (mode 600) into .netci-gate/corp/pki (mode 700)
# and never printed. Leaf certs get 397 days (browser max) and a random serial.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PKI_DIR="${ROOT}/.netci-gate/corp/pki"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

create_ca() {
  # Restrictive directory permissions: private keys live here.
  mkdir -p "${PKI_DIR}" && chmod 700 "${PKI_DIR}"

  # Never overwrite an existing CA: existing server certs and clients trust this root.
  if [[ -f "${PKI_DIR}/ca.key" ]]; then
    log "CA key already exists: ${PKI_DIR}/ca.key"
    exit 0
  fi

  log "creating CA private key and certificate"
  local tmp_dir
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "${tmp_dir}"' EXIT

  # ECDSA P-256 private key under umask 077 for mode 600
  ( umask 077; openssl ecparam -name prime256v1 -genkey -noout -out "${PKI_DIR}/ca.key" )
  chmod 600 "${PKI_DIR}/ca.key"

  # Extensions: CA:TRUE with pathlen:0 so this CA can only sign end-entity certificates.
  cat > "${tmp_dir}/ca.ext" <<'EOF'
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
EOF

  if ! openssl req -new -sha256 \
    -key "${PKI_DIR}/ca.key" \
    -subj "/O=netCI corp lab/CN=netCI corp lab CA" \
    -out "${tmp_dir}/ca.csr" 2>"${tmp_dir}/err.log"; then
    cat "${tmp_dir}/err.log" >&2
    exit 1
  fi

  # 1825 days (5 years) root validity, random serial
  if ! openssl x509 -req -sha256 -days 1825 \
    -in "${tmp_dir}/ca.csr" \
    -signkey "${PKI_DIR}/ca.key" \
    -extfile "${tmp_dir}/ca.ext" \
    -set_serial "0x$(openssl rand -hex 16)" \
    -out "${PKI_DIR}/ca.crt" >/dev/null 2>"${tmp_dir}/err.log"; then
    cat "${tmp_dir}/err.log" >&2
    exit 1
  fi
  chmod 644 "${PKI_DIR}/ca.crt"

  rm -rf "${tmp_dir}"
  trap - EXIT

  log "CA created: ${PKI_DIR}/ca.crt"
}

issue_cert() {
  # Cannot issue leaf certificates without the root CA key and certificate.
  if [[ ! -f "${PKI_DIR}/ca.key" || ! -f "${PKI_DIR}/ca.crt" ]]; then
    echo "error: CA does not exist at ${PKI_DIR} (run '$0 ca' first)" >&2
    exit 1
  fi

  local name="${1:-}"
  # A file name inside the PKI directory, never a path: "../x" would write a key elsewhere.
  if [[ ! "${name}" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; then
    echo "usage: $0 issue <name> <san>...  (name: lowercase letters, digits, dashes)" >&2
    exit 2
  fi
  shift

  if [[ $# -eq 0 ]]; then
    echo "usage: $0 issue <name> <san>..." >&2
    exit 2
  fi

  # Strict SAN validation: each argument must start with DNS: or IP:.
  for san in "$@"; do
    case "${san}" in
      DNS:*|IP:*)
        # Hostname or address characters only: the value is written into an openssl config.
        [[ "${san#*:}" =~ ^[A-Za-z0-9.*:-]+$ ]] || { echo "error: invalid SAN value '${san}'" >&2; exit 2; }
        ;;
      *)
        echo "error: invalid SAN '${san}' (must start with DNS: or IP:)" >&2
        exit 2
        ;;
    esac
  done

  local san_list
  san_list="$(IFS=,; echo "$*")"

  local tmp_dir
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "${tmp_dir}"' EXIT

  # Fresh ECDSA P-256 private key for renewal under umask 077
  # Key and certificate are made in the temp dir and replace the live pair only once the
  # certificate verifies: a failed renewal must leave the service's working pair alone.
  ( umask 077; openssl ecparam -name prime256v1 -genkey -noout -out "${tmp_dir}/${name}.key" )

  # Extensions: leaf certificate with browser maximum 397-day validity
  cat > "${tmp_dir}/${name}.ext" <<EOF
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = ${san_list}
EOF

  if ! openssl req -new -sha256 \
    -key "${tmp_dir}/${name}.key" \
    -subj "/CN=${name}" \
    -out "${tmp_dir}/${name}.csr" 2>"${tmp_dir}/err.log"; then
    cat "${tmp_dir}/err.log" >&2
    exit 1
  fi

  if ! openssl x509 -req -sha256 -days 397 \
    -in "${tmp_dir}/${name}.csr" \
    -CA "${PKI_DIR}/ca.crt" \
    -CAkey "${PKI_DIR}/ca.key" \
    -set_serial "0x$(openssl rand -hex 16)" \
    -extfile "${tmp_dir}/${name}.ext" \
    -out "${tmp_dir}/${name}.crt" >/dev/null 2>"${tmp_dir}/err.log"; then
    cat "${tmp_dir}/err.log" >&2
    exit 1
  fi
  if ! openssl verify -CAfile "${PKI_DIR}/ca.crt" "${tmp_dir}/${name}.crt" >/dev/null; then
    echo "certificate verification failed; the existing ${name} pair is unchanged" >&2
    exit 1
  fi
  install -m 600 "${tmp_dir}/${name}.key" "${PKI_DIR}/${name}.key"
  install -m 644 "${tmp_dir}/${name}.crt" "${PKI_DIR}/${name}.crt"
  # Leaf then CA, for servers that must present the chain.
  cat "${PKI_DIR}/${name}.crt" "${PKI_DIR}/ca.crt" > "${PKI_DIR}/${name}-chain.crt"
  chmod 644 "${PKI_DIR}/${name}-chain.crt"

  rm -rf "${tmp_dir}"
  trap - EXIT

  # Print cert metadata and verify against the CA
  openssl x509 -in "${PKI_DIR}/${name}.crt" -noout -subject
  openssl x509 -in "${PKI_DIR}/${name}.crt" -noout -ext subjectAltName
  openssl x509 -in "${PKI_DIR}/${name}.crt" -noout -enddate
  openssl x509 -in "${PKI_DIR}/${name}.crt" -noout -fingerprint -sha256
  if ! ( cd "${PKI_DIR}" && openssl verify -CAfile ca.crt "${name}.crt" ); then
    echo "certificate verification failed" >&2
    exit 1
  fi
}

case "${1:-}" in
  ca)
    create_ca
    ;;
  issue)
    shift
    issue_cert "$@"
    ;;
  *)
    echo "usage: $0 ca | issue <name> <san>..." >&2
    exit 2
    ;;
esac
