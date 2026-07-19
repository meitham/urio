#!/usr/bin/env bash
# Build urio wheels for multiple targets from a single Linux host using podman.
#
# Produces, into ./dist:
#   - Linux  x86_64   (manylinux, native io_uring backend)
#   - Linux  aarch64  (manylinux, native io_uring backend, via zig)
#   - Windows x86_64   (MSVC, cross-compiled via cargo-xwin; thread backend)
#
# macOS wheels need the macOS SDK and are not cross-buildable from Linux without
# osxcross; build those on a Mac or a macOS CI runner (see .github/workflows).
#
# Requires: podman (or docker), network access to the container registry.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUST_IMAGE="${RUST_IMAGE:-docker.io/rust:1-bookworm}"
ENGINE="${ENGINE:-podman}"
mkdir -p "$REPO/dist"

run() { "$ENGINE" run --rm -v "$REPO":/io:Z -w /io "$RUST_IMAGE" bash -c "$1"; }

echo ">>> Linux x86_64 + aarch64 + armv7 (manylinux, native io_uring)"
run '
set -e
apt-get update -qq >/dev/null && apt-get install -y -qq python3 python3-venv >/dev/null
python3 -m venv /tmp/v && . /tmp/v/bin/activate
pip install -q maturin ziglang
rustup target add aarch64-unknown-linux-gnu armv7-unknown-linux-gnueabihf >/dev/null
export CARGO_TARGET_DIR=/tmp/cargo
maturin build --release --out /io/dist --target x86_64-unknown-linux-gnu --zig
# aarch64 at manylinux2014 (glibc 2.17) so the wheel runs on old-glibc ARM such
# as a Jetson Nano (Ubuntu 18.04, glibc 2.27) — validated there on kernel 4.9
# (thread-backend fallback). zig targets the requested glibc.
maturin build --release --out /io/dist --target aarch64-unknown-linux-gnu --zig --compatibility manylinux2014
# armv7 (32-bit ARM, e.g. Raspberry Pi). The io-uring crate ships no 32-bit-ARM
# syscall bindings, so skip its arch check: the io_uring ABI structs are all
# fixed-width, so the prebuilt bindings are layout-correct. Validated on a Pi
# (armv7l, kernel 5.15) — io_uring works and beats aiofiles on reads/fsync.
RUSTFLAGS="--cfg io_uring_skip_arch_check" maturin build --release --out /io/dist --target armv7-unknown-linux-gnueabihf --zig --compatibility manylinux2014
'

echo ">>> Windows x86_64 (MSVC, cross via cargo-xwin; thread backend)"
run '
set -e
apt-get update -qq >/dev/null && apt-get install -y -qq python3 python3-venv clang llvm lld >/dev/null
python3 -m venv /tmp/v && . /tmp/v/bin/activate
pip install -q maturin cargo-xwin
rustup target add x86_64-pc-windows-msvc >/dev/null
export CARGO_TARGET_DIR=/tmp/cargo
maturin build --release --out /io/dist --target x86_64-pc-windows-msvc
'

echo ">>> done. Wheels in $REPO/dist:"
ls -1 "$REPO/dist"/*.whl
