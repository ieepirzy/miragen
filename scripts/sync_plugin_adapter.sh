#!/usr/bin/env bash
# The Claude Code plugin ships a vendored copy of the stdlib adapter so that
# installing the plugin (locally or as a synced plugin in a cloud session)
# needs nothing else. Run after changing miragen_hook/ or the plugin's skill;
# the sync test pins both.
#   plugins/miragen-memory/skills/  → miragen_hook/skills/   (the daemon installs
#                                     them into Codex/Grok from the package)
#   miragen_hook/                   → plugins/miragen-memory/miragen_hook/
set -euo pipefail
cd "$(dirname "$0")/.."
rsync -a --delete --exclude __pycache__ plugins/miragen-memory/skills/ miragen_hook/skills/
rsync -a --delete --exclude __pycache__ miragen_hook/ plugins/miragen-memory/miragen_hook/
echo "miragen_hook/skills and plugins/miragen-memory/miragen_hook synced"
