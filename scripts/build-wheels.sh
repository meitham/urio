#!/usr/bin/env bash
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
maturin build --release --out /io/dist --target aarch64-unknown-linux-gnu --zig --compatibility manylinux2014
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
