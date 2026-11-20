#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jan_sync.py — Push vault notes straight into Jan project databases.

The Jan app (0.8.x) indexes project files into
`<jan-data>/db/project_<ULID>.db` with the schema:

    files(id, path UNIQUE, name, type, size, chunk_count)
    chunks(id, text, embedding BLOB, file_id, chunk_file_order)

- `files.path` points at the ORIGINAL file on disk (Jan does not copy
  files), so a vault note can be registered without opening Jan.
- chunks are a hard 512-char split of the whole file content, no overlap
  (verified against a 637-chunk project produced by Jan itself).
- embeddings are 1024 float32 little-endian (bge-m3 via llama.cpp).

Usage from webui / MCP:
    list_projects()            -> [{id, name, db, files, chunks}]
    project_state(db, paths)   -> {path: {name, chunk_count}} subset
    sync_note(db, note_path)   -> {"ok", "chunks", "file_id"}
    sync_all(db, vault)        -> {"synced": [...], "skipped": [...]}

Embeddings reuse server_extra._embed_texts (+ its llama-server spawn).
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import time
import struct
import uuid

JAN_DATA = os.path.expanduser("~/Library/Application Support/Jan/data")
DB_DIR = os.path.join(JAN_DATA, "db")
CHUNK = 512


def _project_refs(pid: str) -> tuple[int, str]:
    """(#threads referencing pid, name from the newest thread metadata)."""
    import glob as _g
    refs, name = 0, ""
    for tj in sorted(_g.glob(os.path.join(JAN_DATA, "threads", "*",
                                          "thread.json"))):
        try:
            with open(tj, encoding="utf-8") as f:
                proj = (json.load(f).get("metadata", {}) or {}).get("project") or {}
        except (json.JSONDecodeError, OSError):
            continue
        if proj.get("id") == pid:
            refs += 1
            name = proj.get("name") or name
    return refs, name


JAN_WEBKIT = os.path.expanduser("~/Library/WebKit/jan.ai.app/WebsiteData")


def jan_folders() -> dict[str, str] | None:
    """Danh sách project THẬT của Jan: {id: name}.

    Jan 0.8.x lưu project ("folders") trong localStorage của webview
    (key `thread-management`), không phải trong data/. Đọc từ bản sao
    (db + wal) để không đụng vào file Jan đang mở. None nếu không đọc được.
    """
    import shutil
    import tempfile
    cands = glob.glob(os.path.join(JAN_WEBKIT, "Default", "*", "*",
                                   "LocalStorage", "localstorage.sqlite3"))
    best = None
    for src in sorted(cands, key=os.path.getmtime, reverse=True):
        tmp = tempfile.mkdtemp(prefix="jan_ls_")
        try:
            for ext in ("", "-wal", "-shm"):
                if os.path.exists(src + ext):
                    shutil.copy(src + ext, os.path.join(tmp, "ls.sqlite3" + ext))
            con = sqlite3.connect(os.path.join(tmp, "ls.sqlite3"))
            try:
                row = con.execute("SELECT value FROM ItemTable WHERE key=?",
                                  ("thread-management",)).fetchone()
            finally:
                con.close()
            if not row:
                continue
            raw = row[0].decode("utf-16-le") if isinstance(row[0], bytes) else row[0]
            folders = (json.loads(raw).get("state") or {}).get("folders") or []
            best = {f["id"]: f.get("name") or f["id"] for f in folders if f.get("id")}
            break
        except (sqlite3.Error, OSError, ValueError, KeyError):
            continue
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return best


def _db_counts(fp: str) -> tuple[int, int]:
    if not os.path.isfile(fp) or os.path.getsize(fp) == 0:
        return 0, 0
    try:
        con = sqlite3.connect(fp, timeout=5)  # read-only count, KHÔNG init schema
        try:
            return (con.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                    con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        except sqlite3.Error:  # db có dữ liệu nhưng chưa schema (hiếm)
            return 0, 0
        finally:
            con.close()
    except sqlite3.Error:
        return 0, 0


def list_projects() -> list[dict]:
    """Jan projects + leaked DB files of deleted ones.

    Nguồn chuẩn là jan_folders() (danh sách project trong Jan). `alive` =
    project còn trong Jan; DB không có trong danh sách = project đã xoá mà
    Jan để quên file. Không đọc được danh sách thì đoán qua thread như cũ.
    """
    folders = jan_folders()
    dbs = {os.path.basename(fp)[len("project_"):-len(".db")]: fp
           for fp in glob.glob(os.path.join(DB_DIR, "project_*.db"))}
    out = []
    if folders is not None:
        for pid, name in folders.items():
            fp = dbs.get(pid) or os.path.join(DB_DIR, f"project_{pid}.db")
            files, chunks = _db_counts(fp)
            out.append({"id": pid, "name": name, "alive": True, "db": fp,
                        "files": files, "chunks": chunks,
                        "new": not os.path.isfile(fp) or os.path.getsize(fp) == 0})
        for pid, fp in sorted(dbs.items()):
            if pid in folders:
                continue
            files, chunks = _db_counts(fp)
            out.append({"id": pid, "name": _project_refs(pid)[1] or "",
                        "alive": False, "db": fp, "files": files, "chunks": chunks})
        return out
    # fallback: đoán qua thread metadata (+ ân hạn 2h cho project mới)
    for pid, fp in sorted(dbs.items()):
        files, chunks = _db_counts(fp)
        refs, name = _project_refs(pid)
        fresh = time.time() - os.path.getmtime(fp) < 7200
        out.append({"id": pid, "name": name or pid, "refs": refs,
                    "alive": refs > 0 or fresh or os.path.getsize(fp) == 0,
                    "db": fp, "files": files, "chunks": chunks})
    return out


def purge_project(pid: str) -> str:
    """Move a DEAD project db to ~/.Trash (recoverable). Refuses live ones."""
    for p in list_projects():
        if p["id"] == pid:
            if p["alive"]:
                raise ValueError("project còn sống trong Jan — không xoá được")
            dst = os.path.expanduser(f"~/.Trash/{os.path.basename(p['db'])}")
            i = 1
            while os.path.exists(dst):
                dst = os.path.expanduser(
                    f"~/.Trash/{os.path.basename(p['db'])}.{i}")
                i += 1
            os.rename(p["db"], dst)
            return dst
    raise FileNotFoundError(f"không có project {pid}")


# ---------------------------------------------------------------------------
# Jan embedding model check — vectors we write are bge-m3 (1024-dim, CLS
# pooling). If Jan still embeds queries with its stock MiniLM (384-dim, mean
# pooling) every synced chunk is unreachable by RAG and NOTHING errors out.
# ---------------------------------------------------------------------------

JAN_EMB_DIR = os.path.join(JAN_DATA, "llamacpp", "models",
                           "sentence-transformer-mini")
EXPECTED_DIM = 1024
_EMB_STATUS_CACHE: dict = {}


def _gguf_embedding_length(fp: str) -> int | None:
    """Read `<arch>.embedding_length` from a GGUF header (no deps)."""
    fixed = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1,
             10: 8, 11: 8, 12: 8}

    def u32(f):
        return struct.unpack("<I", f.read(4))[0]

    def u64(f):
        return struct.unpack("<Q", f.read(8))[0]

    def skip(f, t):
        if t in fixed:
            f.seek(fixed[t], 1)
        elif t == 8:
            f.seek(u64(f), 1)
        elif t == 9:
            et, n = u32(f), u64(f)
            if et in fixed:
                f.seek(fixed[et] * n, 1)
            else:
                for _ in range(n):
                    skip(f, et)
        else:
            raise ValueError(f"unknown gguf type {t}")

    with open(fp, "rb") as f:
        if f.read(4) != b"GGUF":
            return None
        u32(f)          # version
        u64(f)          # tensor count
        for _ in range(u64(f)):
            key = f.read(u64(f)).decode("utf-8", "replace")
            t = u32(f)
            if key.endswith(".embedding_length") and t in (4, 5, 10, 11):
                n = fixed[t]
                fmt = {4: "<I", 5: "<i", 10: "<Q", 11: "<q"}[t]
                return struct.unpack(fmt, f.read(n))[0]
            skip(f, t)
    return None


def embed_status() -> dict:
    """{"ok", "dim", "pooling", "reason"} for Jan's project embedding model."""
    gguf = os.path.join(JAN_EMB_DIR, "model.gguf")
    yml = os.path.join(JAN_EMB_DIR, "model.yml")
    if not os.path.isfile(gguf):
        return {"ok": False, "dim": None, "pooling": None,
                "reason": ("Không thấy model embedding của Jan "
                           f"({gguf}). Mở Jan một lần để nó tải model, "
                           "rồi chạy lại install.sh.")}
    st = os.stat(gguf)
    key = (st.st_mtime, st.st_size,
           os.path.getmtime(yml) if os.path.isfile(yml) else 0)
    if _EMB_STATUS_CACHE.get("key") == key:
        return _EMB_STATUS_CACHE["val"]
    try:
        dim = _gguf_embedding_length(gguf)
    except (OSError, ValueError, struct.error):
        dim = None
    pooling = None
    if os.path.isfile(yml):
        for line in open(yml, encoding="utf-8", errors="replace"):
            if line.strip().startswith("pooling:"):
                pooling = line.split(":", 1)[1].strip()
    fix = ("Thay model.gguf trong thư mục sentence-transformer-mini của Jan "
           "bằng bge-m3-Q8_0.gguf (đặt pooling: cls trong model.yml), "
           "restart Jan rồi bấm Đồng bộ tất cả.")
    if dim != EXPECTED_DIM:
        val = {"ok": False, "dim": dim, "pooling": pooling,
               "reason": (f"Model embedding của Jan là {dim or '?'} chiều, "
                          f"note sync vào cần {EXPECTED_DIM} chiều (bge-m3) — "
                          "Jan sẽ không tìm thấy gì. " + fix)}
    elif pooling and pooling != "cls":
        val = {"ok": False, "dim": dim, "pooling": pooling,
               "reason": (f"Jan đang dùng pooling: {pooling}, bge-m3 cần "
                          "pooling: cls — kết quả RAG sẽ lệch. " + fix)}
    else:
        val = {"ok": True, "dim": dim, "pooling": pooling, "reason": ""}
    _EMB_STATUS_CACHE.update(key=key, val=val)
    return val


SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    name TEXT,
    type TEXT,
    size INTEGER,
    chunk_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    embedding BLOB NOT NULL,
    file_id TEXT,
    chunk_file_order INTEGER,
    FOREIGN KEY (file_id) REFERENCES files(id)
);
CREATE INDEX IF NOT EXISTS idx_chunks_id ON chunks(id);
CREATE INDEX IF NOT EXISTS idx_chunks_file_id ON chunks(file_id);
CREATE INDEX IF NOT EXISTS idx_chunks_file_order ON chunks(file_id, chunk_file_order);
"""


def _conn(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db, timeout=10)
    con.execute("PRAGMA busy_timeout=10000")
    con.executescript(SCHEMA)  # Jan 0.8.8 tạo DB rỗng 0-byte — tự khởi tạo schema
    return con


def project_state(db: str, paths: list[str]) -> dict[str, dict]:
    """Which of `paths` are already indexed in the project."""
    con = _conn(db)
    try:
        rows = con.execute("SELECT path, name, chunk_count FROM files").fetchall()
    finally:
        con.close()
    have = {r[0]: {"name": r[1], "chunk_count": r[2]} for r in rows}
    return {p: have[p] for p in paths if p in have}


def _embed(texts: list[str]) -> list[list[float]]:
    """bge-m3 embeddings via server_extra (its llama-server bootstrap)."""
    import server_extra as se  # noqa - resolved from repo when running loose
    if not se._ensure_embed_server():
        raise RuntimeError("embedding server không chạy được "
                           "(thiếu model hoặc llama-server)")
    return se._embed_texts(texts)


def sync_note(db: str, note_path: str, force: bool = False) -> dict:
    """Register/refresh one vault note inside a project db (idempotent)."""
    note_path = os.path.abspath(note_path)
    if not os.path.isfile(note_path):
        return {"ok": False, "error": f"không có file: {note_path}"}
    st = embed_status()
    if not st["ok"]:
        return {"ok": False, "error": st["reason"]}
    text = open(note_path, encoding="utf-8", errors="replace").read()
    pieces = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
    pieces = [p for p in pieces if p.strip()]
    con = _conn(db)
    try:
        old = con.execute("SELECT id FROM files WHERE path=?",
                          (note_path,)).fetchone()
        if old and not force:
            # So nội dung chunk thay vì size: sửa lỗi OCR giữ nguyên số byte
            # (0->O, đổi dấu) vẫn phải re-index.
            stored = [r[0] for r in con.execute(
                "SELECT text FROM chunks WHERE file_id=? "
                "ORDER BY chunk_file_order", (old[0],))]
            if stored == pieces:
                return {"ok": True, "chunks": 0, "file_id": old[0],
                        "skipped": True}
        vectors = _embed(pieces) if pieces else []
        file_id = str(uuid.uuid4())
        name = os.path.basename(note_path)
        with con:  # single transaction
            if old:
                con.execute("DELETE FROM chunks WHERE file_id=?", (old[0],))
                con.execute("DELETE FROM files WHERE id=?", (old[0],))
            con.execute(
                "INSERT INTO files(id, path, name, type, size, chunk_count) "
                "VALUES (?,?,?,?,?,?)",
                (file_id, note_path, name, "md",
                 os.path.getsize(note_path), len(pieces)))
            for order, (piece, vec) in enumerate(zip(pieces, vectors)):
                blob = struct.pack("<%df" % len(vec), *vec)
                con.execute(
                    "INSERT INTO chunks(id, text, embedding, file_id, "
                    "chunk_file_order) VALUES (?,?,?,?,?)",
                    (str(uuid.uuid4()), piece, blob, file_id, order))
        return {"ok": True, "chunks": len(pieces), "file_id": file_id,
                "skipped": False, "note": name}
    finally:
        con.close()


def remove_note(db: str, note_path: str) -> bool:
    """Drop one note (by absolute path) from a project index."""
    note_path = os.path.abspath(note_path)
    con = _conn(db)
    try:
        row = con.execute("SELECT id FROM files WHERE path=?",
                          (note_path,)).fetchone()
        if not row:
            return False
        with con:
            con.execute("DELETE FROM chunks WHERE file_id=?", (row[0],))
            con.execute("DELETE FROM files WHERE id=?", (row[0],))
        return True
    finally:
        con.close()


def prune_missing(db: str, vault_root: str) -> list[str]:
    """Remove index entries for VAULT notes that no longer exist (deleted or
    moved). Files outside the vault (added via Jan itself) are untouched."""
    root = os.path.abspath(os.path.expanduser(vault_root)) + os.sep
    con = _conn(db)
    removed = []
    try:
        rows = con.execute("SELECT id, path FROM files").fetchall()
        gone = [(fid, p) for fid, p in rows
                if p and p.startswith(root) and not os.path.isfile(p)]
        with con:
            for fid, p in gone:
                con.execute("DELETE FROM chunks WHERE file_id=?", (fid,))
                con.execute("DELETE FROM files WHERE id=?", (fid,))
                removed.append(os.path.basename(p))
    finally:
        con.close()
    return removed


def sync_all(db: str, vault_root: str, force: bool = False) -> dict:
    """Sync every vault note (Inbox + Notes) into the project db, then drop
    entries of notes that were deleted/moved. Inbox is synced on purpose:
    the web-upload flow writes there and auto-syncs; Notes/ is optional
    organisation, not a review gate."""
    st = embed_status()
    if not st["ok"]:
        return {"synced": [], "skipped": [], "failed": [], "removed": [],
                "error": st["reason"]}
    paths = []
    for sub in ("Inbox", "Notes"):
        d = os.path.join(vault_root, sub)
        if os.path.isdir(d):
            paths += [os.path.join(d, n) for n in sorted(os.listdir(d))
                      if n.lower().endswith(".md")]
    synced, skipped, failed = [], [], []
    for p in paths:
        r = sync_note(db, p, force=force)
        if not r.get("ok"):
            failed.append({"note": os.path.basename(p),
                           "error": r.get("error", "?")})
        elif r.get("skipped"):
            skipped.append(os.path.basename(p))
        else:
            synced.append({"note": os.path.basename(p),
                           "chunks": r["chunks"]})
    removed = prune_missing(db, vault_root)
    return {"synced": synced, "skipped": skipped, "failed": failed,
            "removed": removed}


if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser()
    ap.add_argument("note", nargs="?", help="note .md cần đồng bộ")
    ap.add_argument("--db", help="project db (mặc định: project đầu tiên)")
    ap.add_argument("--list", action="store_true", help="liệt kê projects")
    ap.add_argument("--all", action="store_true", help="sync cả vault")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--embed-check", action="store_true",
                    help="kiểm tra model embedding của Jan có phải bge-m3")
    a = ap.parse_args()
    if a.embed_check:
        print(json.dumps(embed_status(), ensure_ascii=False, indent=1))
        sys.exit(0)
    if a.list:
        print(json.dumps(list_projects(), ensure_ascii=False, indent=1))
        sys.exit(0)
    projs = list_projects()
    if not projs:
        print("Không thấy project Jan nào."); sys.exit(1)
    db = a.db or projs[0]["db"]
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    if a.all:
        vault = os.environ.get("PDF_OCR_VAULT",
                               os.path.expanduser("~/Documents/Vault"))
        print(json.dumps(sync_all(db, vault, force=a.force),
                         ensure_ascii=False, indent=1))
    elif a.note:
        print(json.dumps(sync_note(db, a.note, force=a.force),
                         ensure_ascii=False, indent=1))
    else:
        ap.print_help()
