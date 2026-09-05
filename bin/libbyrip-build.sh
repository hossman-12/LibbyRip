#!/bin/bash
# libbyrip-build - launcher for auto_build_m4b.py.
#
# Why this launcher exists:
#   This repo is often edited on Windows (where PowerShell is the primary
#   workflow via ``auto_built-m4b.ps1``) but is also run on Debian 13 /
#   Linux. When the repo lives on a rclone/FUSE mount (e.g. OneDrive),
#   rclone silently strips the execute bit on every file, so invoking the
#   script directly with ``./auto_build_m4b.py`` fails with::
#
#       /usr/bin/env: bad interpreter: Permission denied
#
#   even after ``chmod +x``. Running the script through ``python3``
#   bypasses that limitation, because Python reads the file as ordinary
#   data and does not require it to be executable.
#
# Installation (one-time, from the repo root):
#
#       mkdir -p ~/.local/bin
#       install -m 0755 bin/libbyrip-build.sh ~/.local/bin/libbyrip-build
#       # add to PATH in ~/.bashrc if not already there:
#       #   if [ -d "$HOME/.local/bin" ]; then PATH="$HOME/.local/bin:$PATH"; fi
#
# Usage (from anywhere):
#
#       libbyrip-build                 # full conversion run
#       libbyrip-build --dry-run       # preview only
#       libbyrip-build --detail        # show queued zip paths
#       libbyrip-build --help
#
# You can also call it directly without installing:
#
#       bash bin/libbyrip-build.sh --dry-run
#
# The launcher resolves its own location (``BASH_SOURCE``) and invokes
# ``python3`` against the sibling ``auto_build_m4b.py``, so it works no
# matter where the repo lives on disk.

set -euo pipefail

# Resolve the directory containing this launcher, following symlinks so the
# launcher works whether invoked directly or through a symlink in PATH.
LAUNCHER_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_DIR="$(dirname "$(dirname "${LAUNCHER_PATH}")")"
SCRIPT="${REPO_DIR}/auto_build_m4b.py"

if [[ ! -f "${SCRIPT}" ]]; then
    echo "libbyrip-build: cannot find ${SCRIPT}" >&2
    echo "  Is auto_build_m4b.py still in the repo root?" >&2
    echo "  This launcher must live in <repo>/bin/libbyrip-build.sh." >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "libbyrip-build: python3 is not on PATH. Install it with:" >&2
    echo "  sudo apt install python3" >&2
    exit 127
fi

exec python3 "${SCRIPT}" "$@"