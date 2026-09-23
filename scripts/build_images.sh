#!/usr/bin/env bash
# Build netCI's three images and push them to a registry the cluster pulls from.
#
#   scripts/build_images.sh harbor.example.com/netci 0.2.0
#
# All three build from the repository root: the API image carries the migrations and
# their runner, the worker carries the playbooks and the workload chart. Set
# DOCKER_BUILD_ARGS="--network host" where the Docker daemon has no bridge network.
set -euo pipefail

registry="${1:?usage: build_images.sh <registry>/<path> <tag>}"
tag="${2:?usage: build_images.sh <registry>/<path> <tag>}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
extra=(${DOCKER_BUILD_ARGS:-})

# The library copy of the CI tooling must match its sources before anything ships.
python3 "${root}/scripts/sync_shared_library.py" --check

docker build "${extra[@]}" -f "${root}/backend/Dockerfile" -t "${registry}/backend:${tag}" "${root}"
docker build "${extra[@]}" -f "${root}/backend/Dockerfile.worker" -t "${registry}/worker:${tag}" "${root}"
docker build "${extra[@]}" -t "${registry}/frontend:${tag}" "${root}/frontend"

for image in backend worker frontend; do
  docker push "${registry}/${image}:${tag}"
done
echo "pushed ${registry}/{backend,worker,frontend}:${tag}"
