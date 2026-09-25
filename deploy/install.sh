#!/usr/bin/env bash
# Install one immutable Inference Engine release on a supported Linux host.
#
# This script is shipped as a GitHub release asset. It downloads the release
# Compose file and settings template, and verifies both against that release's
# SHA256SUMS before Docker pulls the digest-pinned image. To verify this script
# itself before execution, follow the separately verified bootstrap in the docs.
set -euo pipefail

readonly REPOSITORY="dlroqa/inference-engine"
readonly RELEASES_URL="https://github.com/${REPOSITORY}/releases/download"

usage() {
  cat <<'EOF'
Usage: install.sh vMAJOR.MINOR.PATCH

Set IE_INSTALL_DIR to choose the destination (default:
./inference-engine-vMAJOR.MINOR.PATCH). The destination's existing
inference-engine.env is never overwritten.
EOF
}

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

[[ $# -eq 1 ]] || { usage >&2; exit 2; }
readonly VERSION="$1"
[[ "$VERSION" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || {
  fail "release must be vMAJOR.MINOR.PATCH, got: $VERSION"
}

[[ "$(uname -s)" == "Linux" ]] || fail "only Linux hosts are supported"
case "$(uname -m)" in
  x86_64|amd64) ;;
  *) fail "only Linux x86-64 hosts are supported (found $(uname -m))" ;;
esac
grep -qw avx2 /proc/cpuinfo || fail "this CPU lacks AVX2, required by the llama.cpp backend"

require_command curl
require_command sha256sum
require_command docker
docker compose version >/dev/null 2>&1 || fail "Docker Compose plugin is required"

readonly INSTALL_DIR="${IE_INSTALL_DIR:-$PWD/inference-engine-$VERSION}"
mkdir -p "$INSTALL_DIR"
[[ -d "$INSTALL_DIR" ]] || fail "installation path is not a directory: $INSTALL_DIR"
readonly TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/inference-engine-install.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

download() {
  curl --fail --location --proto '=https' --tlsv1.2 --silent --show-error \
    "${RELEASES_URL}/${VERSION}/$1" --output "${TMP_DIR}/$1"
}

printf 'Downloading and verifying release %s...\n' "$VERSION"
download SHA256SUMS
download compose.yaml
download inference-engine.env.example

for asset in compose.yaml inference-engine.env.example; do
  grep -Eq "^[0-9a-f]{64}  ${asset}$" "${TMP_DIR}/SHA256SUMS" || \
    fail "SHA256SUMS has no checksum for ${asset}"
done
(cd "$TMP_DIR" && sha256sum --strict --check --ignore-missing SHA256SUMS)

cp "${TMP_DIR}/compose.yaml" "${INSTALL_DIR}/compose.yaml"
if [[ ! -e "${INSTALL_DIR}/inference-engine.env" ]]; then
  cp "${TMP_DIR}/inference-engine.env.example" "${INSTALL_DIR}/inference-engine.env"
else
  printf 'Keeping existing %s/inference-engine.env\n' "$INSTALL_DIR"
fi

printf 'Pulling the digest-pinned image and starting the service...\n'
docker compose -f "${INSTALL_DIR}/compose.yaml" pull
docker compose -f "${INSTALL_DIR}/compose.yaml" up --detach

cat <<EOF

Installed ${VERSION} in ${INSTALL_DIR}.

Create the owner key now (it is printed once; save it securely):
  docker compose -f "${INSTALL_DIR}/compose.yaml" exec inference-engine \\
    inference-engine keys create --label owner

The dashboard is available locally at http://127.0.0.1:8000/dashboard.
EOF
