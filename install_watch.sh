#!/bin/bash
# -*- coding: utf-8 -*-
# Cài user LaunchAgent tự động OCR file thả vào Vault/_dropbox.
# Vault lấy từ env PDF_OCR_VAULT (mặc định ~/Documents/Vault).
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
VAULT="${PDF_OCR_VAULT:-$HOME/Documents/Vault}"
PLIST=~/Library/LaunchAgents/ai.jan.visdocs.watch.plist
mkdir -p ~/Library/LaunchAgents "$(dirname "$PLIST")"
mkdir -p "$VAULT/_dropbox" "$VAULT/Inbox" "$VAULT/Notes" "$VAULT/Attachments"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.jan.visdocs.watch</string>
  <key>ProgramArguments</key>
  <array>
    <string>${DIR}/.venv/bin/python</string>
    <string>${DIR}/watch_folder.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PDF_OCR_VAULT</key><string>${VAULT}</string>
  </dict>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>${DIR}/watch.log</string>
  <key>StandardErrorPath</key><string>${DIR}/watch.log</string>
</dict>
</plist>
EOF
launchctl bootout gui/$(id -u) "$PLIST" 2>/dev/null || true
launchctl bootstrap gui/$(id -u) "$PLIST"
launchctl kickstart -k gui/$(id -u)/ai.jan.visdocs.watch
echo "Đã cài launchd agent: $PLIST"
echo "Vault watch: $VAULT/_dropbox"
