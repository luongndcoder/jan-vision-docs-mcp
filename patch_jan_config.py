#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Idempotently register the pdf-ocr MCP server in Jan's mcp_config.json.

Usage:
    python patch_jan_config.py --server-cmd <venv-python> --server-script <server.py> \
        [--config <mcp_config.json>] [--langs vi-VN,en-US] [--vault <path>] [--no-backup]

- Creates the file (with a minimal skeleton) if Jan has never configured MCP.
- Updates the existing `pdf-ocr` entry in place; other servers are untouched.
- Writes a timestamped .bak next to the config on first modification.
- Exit 0 on success, 1 on error. Prints the resulting entry as JSON.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

DEFAULT_CONFIG = os.path.expanduser(
    "~/Library/Application Support/Jan/data/mcp_config.json")
KEY = "pdf-ocr"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--server-cmd", required=True,
                    help="absolute path of the venv python interpreter")
    ap.add_argument("--server-script", required=True,
                    help="absolute path of server.py")
    ap.add_argument("--langs", default="vi-VN,en-US")
    ap.add_argument("--vault", default=os.path.expanduser("~/Documents/Vault"))
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    cfg_path = os.path.expanduser(args.config)
    cfg: dict = {}
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"ERROR: cannot parse {cfg_path}: {e}", file=sys.stderr)
            return 1
    else:
        cfg = {"mcpServers": {}, "mcpSettings": {"toolCallTimeoutSeconds": 180}}

    if not isinstance(cfg.get("mcpServers"), dict):
        cfg["mcpServers"] = {}

    entry = {
        "command": os.path.abspath(args.server_cmd),
        "args": [os.path.abspath(args.server_script)],
        "env": {
            "PDF_OCR_LANGS": args.langs,
            "PDF_OCR_VAULT": os.path.abspath(args.vault),
        },
        "active": True,
    }

    old = cfg["mcpServers"].get(KEY)
    cfg["mcpServers"][KEY] = entry

    if os.path.isfile(cfg_path) and not args.no_backup:
        bak = cfg_path + datetime.datetime.now().strftime(".bak.%Y%m%d-%H%M%S")
        try:
            os.replace(cfg_path, bak)
            print(f"backup: {bak}")
        except OSError as e:
            print(f"WARN: backup failed: {e}", file=sys.stderr)

    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, cfg_path)

    action = "updated" if old else "added"
    print(f"{action} `{KEY}` in {cfg_path}")
    print(json.dumps(entry, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
