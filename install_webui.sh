#!/bin/bash
# -*- coding: utf-8 -*-
# Cài LaunchAgent cho web UI upload (localhost:8765).
# Vault lấy từ env PDF_OCR_VAULT (mặc định ~/Documents/Vault), port PDF_OCR_WEBUI_PORT.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
VAULT="${PDF_OCR_VAULT:-$HOME/Documents/Vault}"
PORT="${PDF_OCR_WEBUI_PORT:-8765}"
PLIST=~/Library/LaunchAgents/ai.jan.visdocs.webui.plist
mkdir -p ~/Library/LaunchAgents "$(dirname "$PLIST")"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.jan.visdocs.webui</string>
  <key>ProgramArguments</key>
  <array>
    <string>${DIR}/.venv/bin/python</string>
    <string>${DIR}/webui.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PDF_OCR_VAULT</key><string>${VAULT}</string>
    <key>PDF_OCR_WEBUI_PORT</key><string>${PORT}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${DIR}/webui.log</string>
  <key>StandardErrorPath</key><string>${DIR}/webui.log</string>
</dict>
</plist>
PLISTEOF
launchctl bootout gui/$(id -u) "$PLIST" 2>/dev/null || true
launchctl bootstrap gui/$(id -u) "$PLIST"
echo "Đã cài launchd agent web UI: $PLIST"
echo "Mở: http://127.0.0.1:${PORT}  (vault: ${VAULT})"
