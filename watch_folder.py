#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Watch ~/Documents/Vault/_dropbox: file nào thả vào là tự OCR (Vision) rồi
ghi note .md vào Vault/Inbox, file gốc chuyển sang _dropbox/_done (lỗi: _failed).

Chạy:  python watch_folder.py           (quét 1 lần rồi thoát — launchd gọi theo interval)
       python watch_folder.py --loop    (chạy nền quét mỗi N giây)
Cài launchd:  bash install_watch.sh
"""
from __future__ import annotations

import os
import shutil
import sys
import time

VAULT = os.environ.get("PDF_OCR_VAULT", os.path.expanduser("~/Documents/Vault"))
DROP = os.path.join(VAULT, "_dropbox")
DONE = os.path.join(DROP, "_done")
FAIL = os.path.join(DROP, "_failed")
HERE = os.path.dirname(os.path.abspath(__file__))
POLL = int(os.environ.get("PDF_OCR_WATCH_POLL", "20"))

SUPPORTED = {".pdf", ".docx", ".md", ".markdown", ".png", ".jpg", ".jpeg",
             ".heic", ".heif", ".tiff", ".tif", ".webp", ".bmp"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def stable_and_old(path: str, min_age_s: float = 15) -> bool:
    """Đợi file copy xong hẳn (kích thước không đổi 2 lần quét liên tiếp + đủ cũ)."""
    try:
        st = os.stat(path)
        if time.time() - st.st_mtime < min_age_s:
            return False
        return True
    except FileNotFoundError:
        return False


def scan_once() -> int:
    if not os.path.isdir(DROP):
        return 0
    sys.path.insert(0, HERE)
    import server  # noqa: E402  (nạp pipeline; không chạy MCP vì không phải __main__)
    ing = server.server_extra.ingest_document

    processed = 0
    for name in sorted(os.listdir(DROP)):
        src = os.path.join(DROP, name)
        if not os.path.isfile(src) or name.startswith(".") or \
                os.path.splitext(name)[1].lower() not in SUPPORTED:
            continue
        if not stable_and_old(src):
            continue
        try:
            import json as _json
            r = _json.loads(ing(src, VAULT, "Inbox", None, 200.0, True))
            log(f"OK  {name} -> {r.get('written')} ({r.get('chars')} chars)")
            os.makedirs(DONE, exist_ok=True)
            os.replace(src, os.path.join(DONE, name))
            processed += 1
        except Exception as e:
            log(f"ERR {name}: {e}")
            os.makedirs(FAIL, exist_ok=True)
            try:
                os.replace(src, os.path.join(FAIL, name))
            except OSError:
                pass
    return processed


def main() -> int:
    if "--loop" in sys.argv:
        lock = os.path.join(HERE, ".watch.lock")
        import fcntl
        try:
            fd = os.open(lock, os.O_CREAT | os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("một watcher khác đang chạy — thoát")
            return 0
        log(f"watching {DROP} (poll {POLL}s)")
        while True:
            scan_once()
            time.sleep(POLL)
    else:
        return 0 if scan_once() >= 0 else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
