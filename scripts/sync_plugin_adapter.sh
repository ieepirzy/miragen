#!/usr/bin/env bash
# The Claude Code plugin ships a vendored copy of the stdlib adapter so that
# installing the plugin (locally or as a synced plugin in a cloud session)
# needs nothing else. Run after changing miragen_hook/; the sync test pins it.
set -euo pipefail
cd "$(dirname "$0")/.."
rsync -a --delete --exclude __pycache__ miragen_hook/ plugins/miragen-memory/miragen_hook/
echo "plugins/miragen-memory/miragen_hook synced"
