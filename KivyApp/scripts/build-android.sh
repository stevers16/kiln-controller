#!/usr/bin/env bash
# One-shot Android build helper. Run from WSL2.
#
# Assumes:
#   - buildozer + cython are installed (`pip3 install --user buildozer cython`)
#   - JDK 17, autoconf, libtool, libffi-dev, libssl-dev etc. are installed
#     (see KivyApp/README.md "One-time setup" for the apt list)
#   - Existing Android SDK + NDK paths are filled into buildozer.spec
#     (android.sdk_path / android.ndk_path lines)
#
# Pass any extra buildozer args through, e.g.
#   ./scripts/build-android.sh -v
#   ./scripts/build-android.sh android release
#
# Default action is `android debug`.

set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v buildozer >/dev/null 2>&1; then
    echo "buildozer not found in PATH" >&2
    echo "Install with: pip3 install --user --upgrade buildozer cython==0.29.36" >&2
    exit 1
fi

if ! grep -E '^android\.sdk_path[[:space:]]*=[[:space:]]*[^[:space:]]' buildozer.spec >/dev/null; then
    echo "warning: android.sdk_path is not set in buildozer.spec" >&2
    echo "buildozer will try to download a fresh SDK. Set the path to skip." >&2
fi

if [[ $# -eq 0 ]]; then
    exec buildozer android debug
else
    exec buildozer "$@"
fi
