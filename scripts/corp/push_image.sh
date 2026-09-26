#!/usr/bin/env bash
# Publish an image from this host's Docker to the lab Harbor over verified TLS; prints the
# digest the registry stored (for pinning).
#
#   scripts/corp/push_image.sh <local image> <harbor reference>
#   scripts/corp/push_image.sh nginx:1.29 172.17.0.1:8930/mirror/nginx:1.29
#
# Why not `docker push`: Docker 29's containerd image store fetches the registry token without
# /etc/docker/certs.d, so a private CA fails there ("x509: certificate signed by unknown
# authority"). Trusting the lab CA machine-wide would make every program on this host trust
# it; instead buildah (from the build toolbox) pushes with --cert-dir holding only the lab CA,
# authenticated as the operators' robot. The image goes through `docker save`, unchanged.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
C="${ROOT}/.netci-gate/corp"
TOOLBOX="${NETCI_TOOLBOX_IMAGE:-netci/ci-toolbox:0.4.0}"
[[ $# -eq 2 ]] || { echo "usage: $0 <local image> <harbor reference>" >&2; exit 2; }
src="$1" dest="$2"
case "${dest}" in 172.17.0.1:8930/*) ;; *) echo "destination must be on the lab Harbor (172.17.0.1:8930/...)" >&2; exit 2 ;; esac

work="$(mktemp -d)"; trap 'rm -rf "${work}"' EXIT
mkdir -m 700 "${work}/certs" "${work}/auth"
cp "${C}/pki/ca.crt" "${work}/certs/ca.crt"
# The robot's credential as a containers-auth file, made with umask 077 and never on a
# command line.
( umask 077
  python3 - "${C}" "${work}/auth/auth.json" <<'EOF'
import base64, json, sys
c, out = sys.argv[1:]
name = open(f"{c}/harbor_ops_robot_name").read().strip()
secret = open(f"{c}/harbor_ops_robot_secret").read().strip()
token = base64.b64encode(f"{name}:{secret}".encode()).decode()
json.dump({"auths": {"172.17.0.1:8930": {"auth": token}}}, open(out, "w"))
EOF
)
docker save "${src}" -o "${work}/image.tar"
chmod 644 "${work}/image.tar"
# buildah unshares namespaces, which Docker's default seccomp/apparmor profiles refuse; this
# short-lived container is the lab's own toolbox, not a build of untrusted code.
docker run --rm --network host --user 0 --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
  -v "${work}:/work" -e STORAGE_DRIVER=vfs -e BUILDAH_ISOLATION=chroot \
  --entrypoint sh "${TOOLBOX}" -c '
    set -e
    id=$(buildah pull -q "docker-archive:/work/image.tar")
    buildah push --quiet --cert-dir /work/certs --authfile /work/auth/auth.json \
      --digestfile /work/digest "$id" "docker://$0"' "${dest}" >/dev/null
cat "${work}/digest"; echo
