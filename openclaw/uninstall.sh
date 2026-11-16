#!/usr/bin/env bash
# Remove FBM's OpenClaw jobs (fbm:*), the fbm skill and what install.sh set up. Same as: install.sh --uninstall
# Takes install.sh's options, e.g. --dry-run, --agent ID, --skill-dir DIR.
set -euo pipefail
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.sh" --uninstall "$@"
