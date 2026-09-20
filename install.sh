#!/usr/bin/env bash
# Installs the jev-review CLI and agent skill for the current user.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p ~/.agents/skills/jev-review
cp skills/jev-review/SKILL.md ~/.agents/skills/jev-review/SKILL.md

mkdir -p ~/.local/bin
cp jev_client.py ~/.local/bin/jev-review
chmod +x ~/.local/bin/jev-review

echo "installed: ~/.agents/skills/jev-review/SKILL.md, ~/.local/bin/jev-review"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "warning: ~/.local/bin is not on PATH — add it to your shell profile" ;;
esac
