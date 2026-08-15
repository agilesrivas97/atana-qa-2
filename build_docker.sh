#!/usr/bin/env bash
# build_docker.sh
# ================
# Builds the Windows .exe artifacts locally, inside a Docker container
# running Wine — no Windows machine needed to produce the binaries. Copy
# dist/exe/*.exe and tools/dist/*.exe to your Windows VM afterwards (to run
# them directly, or to feed installer/atana_setup.iss there with Inno Setup).
#
# Usage:
#   ./build_docker.sh                 # panel + otp + setup (fast, reliable)
#   ./build_docker.sh panel           # just atana_panel.exe
#   ./build_docker.sh dispatcher      # EXPERIMENTAL — see docker/windows-build/README.md
#   ./build_docker.sh all             # everything
#   VERSION=3.2.0 ./build_docker.sh dispatcher
#
# Requires Docker Desktop running. Always builds/runs as linux/amd64 — Wine
# doesn't run reliably on arm64 Linux; Docker Desktop on Apple Silicon
# emulates amd64 transparently (slower on first build, fine afterwards).

set -euo pipefail
cd "$(dirname "$0")"

IMAGE=atana-windows-build

if ! docker info >/dev/null 2>&1; then
    echo "[ERROR] Docker no está corriendo. Abrí Docker Desktop y volvé a intentar." >&2
    exit 1
fi

echo "==> Construyendo la imagen de build (Wine + Python Windows) — puede tardar 10-20 min la primera vez..."
docker build --platform linux/amd64 -t "$IMAGE" -f docker/windows-build/Dockerfile .

echo "==> Corriendo el build dentro del container..."
docker run --rm --platform linux/amd64 \
    -v "$(pwd):/build/atana" \
    -e VERSION="${VERSION:-}" \
    "$IMAGE" "$@"
