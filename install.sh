#!/usr/bin/env bash
# Installs jevkit's clients for the current user: the ask-jev and jev-review
# CLIs, their agent skills, and the Pi ask_jev tool. Plain copies, not
# symlinks — re-run after pulling changes.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p ~/.local/bin
install -m 755 cli/ask_jev.py ~/.local/bin/ask-jev
install -m 755 cli/jev_review.py ~/.local/bin/jev-review
echo "installed: ~/.local/bin/ask-jev, ~/.local/bin/jev-review"

for skills_dir in ~/.claude/skills ~/.agents/skills; do
  [ -d "$skills_dir" ] || continue
  for skill in integrations/skills/*/; do
    name=$(basename "$skill")
    mkdir -p "$skills_dir/$name"
    cp "$skill/SKILL.md" "$skills_dir/$name/SKILL.md"
  done
  echo "installed skills: $skills_dir/{ask-jev,jev-review}"
done

if [ -d ~/.pi/agent/extensions ]; then
  cp integrations/pi/ask-jev.ts ~/.pi/agent/extensions/ask-jev.ts
  echo "installed: ~/.pi/agent/extensions/ask-jev.ts (reload pi)"
fi

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "warning: ~/.local/bin is not on PATH — add it to your shell profile" ;;
esac
