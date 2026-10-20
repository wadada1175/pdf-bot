#!/bin/bash
# Mac mini にログインしたら Bot が自動で起動し、落ちても再起動するように登録する。
# 解除: launchctl bootout gui/$(id -u)/lab.printbot
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="lab.printbot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -x "$DIR/.venv/bin/python" ]; then
  echo "先に README の手順で .venv を作ってください" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DIR/.venv/bin/python</string>
    <string>$DIR/bot.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$DIR/bot.log</string>
  <key>StandardErrorPath</key><string>$DIR/bot.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "登録しました。ログ: $DIR/bot.log"
