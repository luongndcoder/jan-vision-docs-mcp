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


def list_projects() -> list[dict]:
    """Jan project DBs. `alive` = some thread still references it (a db
    whose threads are gone means the project was deleted in Jan but the
    app leaks the file on disk)."""
    out = []
    for fp in sorted(glob.glob(os.path.join(DB_DIR, "project_*.db"))):
        pid = os.path.basename(fp)[len("project_"):-len(".db")]
        if os.path.getsize(fp) == 0:
            continue
        try:
            con = sqlite3.connect(fp, timeout=5)
            files = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            con.close()
        except sqlite3.Error:
            continue
        refs, name = _project_refs(pid)
        out.append({"id": pid, "name": name or pid, "refs": refs,
                    "alive": refs > 0,
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


def _conn(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db, timeout=10)
    con.execute("PRAGMA busy_timeout=10000")
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
    con = _conn(db)
    try:
        old = con.execute("SELECT id FROM files WHERE path=?",
                          (note_path,)).fetchone()
        if old and not force:
            size = os.path.getsize(note_path)
            cur_size = con.execute(
                "SELECT size FROM files WHERE id=?", (old[0],)).fetchone()
            if cur_size and cur_size[0] == size:
                return {"ok": True, "chunks": 0, "file_id": old[0],
                        "skipped": True}
        text = open(note_path, encoding="utf-8", errors="replace").read()
        pieces = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
        pieces = [p for p in pieces if p.strip()]
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


def sync_all(db: str, vault_root: str, force: bool = False) -> dict:
    """Sync every vault note (Inbox + Notes) into the project db."""
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
    return {"synced": synced, "skipped": skipped, "failed": failed}


if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser()
    ap.add_argument("note", nargs="?", help="note .md cần đồng bộ")
    ap.add_argument("--db", help="project db (mặc định: project đầu tiên)")
    ap.add_argument("--list", action="store_true", help="liệt kê projects")
    ap.add_argument("--all", action="store_true", help="sync cả vault")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
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
