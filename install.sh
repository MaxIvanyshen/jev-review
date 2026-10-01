#!/usr/bin/env bash
# Installs the jev-review CLI for the current user. Skill install is agent-
# specific (each coding agent keeps skills in a different place) — see the
# "Agent skill" section in README.md for a prompt that handles that part.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p ~/.local/bin
cp jev_client.py ~/.local/bin/jev-review
chmod +x ~/.local/bin/jev-review
cp ask_jev_cli.py ~/.local/bin/ask-jev
chmod +x ~/.local/bin/ask-jev

echo "installed: ~/.local/bin/jev-review"
echo "installed: ~/.local/bin/ask-jev"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "warning: ~/.local/bin is not on PATH — add it to your shell profile" ;;
esac
