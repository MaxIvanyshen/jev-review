#!/usr/bin/env bash
# Runs Kev (github.com/jaredpalmer/kev, open-weight Jev-like model) on this
# Mac for ask-jev's local private mode: a launchd agent serves it on
# 127.0.0.1:8009, starts it at login and restarts it if it dies.
# Costs ~1GB venv + ~1.6GB weights on disk, ~2GB RAM while running.
# Uninstall: launchctl bootout gui/$(id -u)/com.jevkit.kev; rm ~/Library/LaunchAgents/com.jevkit.kev.plist
set -euo pipefail

KEV_DIR=~/.local/share/kev
KEV_SHA=952ce9d053f6c958d1447c57c36cc9cb3369aef4  # Kev 1.0
MODEL=${KEV_MODEL:-jaredpalmer/kev-0.8b}
LABEL=com.jevkit.kev
PLIST=~/Library/LaunchAgents/$LABEL.plist

UV=$(command -v uv) || { echo "needs uv: brew install uv" >&2; exit 1; }
[ -d "$KEV_DIR" ] || git clone -q https://github.com/jaredpalmer/kev.git "$KEV_DIR"
git -C "$KEV_DIR" fetch -q --depth 1 origin "$KEV_SHA"
git -C "$KEV_DIR" checkout -q "$KEV_SHA"
(cd "$KEV_DIR" && "$UV" sync --quiet --extra serve)

mkdir -p ~/Library/LaunchAgents ~/Library/Logs
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UV</string><string>run</string><string>--extra</string><string>serve</string>
    <string>python</string><string>-m</string><string>kev.serve</string>
    <string>--run</string><string>$MODEL</string><string>--port</string><string>8009</string>
  </array>
  <key>WorkingDirectory</key><string>$KEV_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/kev.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/kev.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "waiting for Kev on 127.0.0.1:8009 (first run downloads ~1.6GB of weights)..."
for _ in $(seq 1 300); do
  if curl -s -m 2 -o /dev/null http://127.0.0.1:8009/v1/models; then
    echo "Kev is up: ask-jev --private now runs on this Mac (log: ~/Library/Logs/kev.log)"
    exit 0
  fi
  sleep 3
done
echo "Kev did not come up in 15 min — see ~/Library/Logs/kev.log" >&2
exit 1
