#!/usr/bin/env bash
# Put uv (both Mac architectures merged into one binary) and the backend wheel into
# the app bundle's resources. Used by .github/workflows/release-mac.yml; run it the
# same way before building locally:
#   desktop/scripts/prepare-resources.sh dist/press_engine-0.2.0-py3-none-any.whl
set -euo pipefail

WHEEL="${1:?usage: prepare-resources.sh path/to/press_engine-X.Y.Z-py3-none-any.whl}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
RES="$HERE/src-tauri/resources"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

UV_VERSION="${UV_VERSION:-latest}"
BASE="https://github.com/astral-sh/uv/releases"
if [[ "$UV_VERSION" == latest ]]; then URL="$BASE/latest/download"; else URL="$BASE/download/$UV_VERSION"; fi

for arch in aarch64 x86_64; do
  curl -fsSL --retry 3 "$URL/uv-$arch-apple-darwin.tar.gz" -o "$TMP/uv-$arch.tar.gz"
  mkdir -p "$TMP/$arch"
  tar -xzf "$TMP/uv-$arch.tar.gz" -C "$TMP/$arch"
done

mkdir -p "$RES/bin" "$RES/backend"
ARM="$(find "$TMP/aarch64" -type f -name uv | head -1)"
X86="$(find "$TMP/x86_64" -type f -name uv | head -1)"
if command -v lipo >/dev/null 2>&1; then
  lipo -create "$ARM" "$X86" -output "$RES/bin/uv"
else
  echo "lipo not found (not on a Mac): bundling the Apple Silicon uv only" >&2
  cp "$ARM" "$RES/bin/uv"
fi
chmod 755 "$RES/bin/uv"

rm -f "$RES/backend/"*.whl
cp "$WHEEL" "$RES/backend/"
echo "Resources ready: $(ls "$RES/bin") + $(ls "$RES/backend" | grep whl)"
