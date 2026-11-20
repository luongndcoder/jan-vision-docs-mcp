"""Persistent hybrid search index (SQLite: vectors + FTS5 BM25) for Vault notes.

Built at ingest time (ensure), queried with 1 embedding + numpy dot + BM25,
fused with RRF. Shared by the MCP server and the web UI (WAL, busy_timeout).
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import sys
import time

import numpy as np

import server_extra as se

DB_DEFAULT = "~/Library/Application Support/jan-vision-docs/index.sqlite"
_PAGE_RE = re.compile(r"^## Trang (\d+)\s*$", re.M)
_TOK = re.compile(r"\w{2,}")
STOP = {"and", "for", "the", "with", "from", "và", "của", "cho", "trong",
        "các", "có", "được", "là", "về", "một", "này", "that", "this",
        "then", "into", "tôi", "anh", "em", "you", "your"}
CAND = 30          # candidates per list
COS_MIN = 0.30
TRUST = 0.6
RRF_K = 60
LOW_MIN_HITS = 2   # an isolated single weak chunk is noise, not evidence
_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes(path TEXT PRIMARY KEY, mtime REAL, size INT);
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, path TEXT, page TEXT,
  ord INT, text TEXT, vec BLOB);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, content='chunks',
  content_rowid='id', tokenize='unicode61 remove_diacritics 0');
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vocab USING fts5vocab(chunks_fts, 'row');
"""


def db_path() -> str:
    return os.path.expanduser(os.environ.get("PDF_OCR_INDEX") or DB_DEFAULT)


def _connect() -> sqlite3.Connection:
    p = db_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    c = sqlite3.connect(p, timeout=10, isolation_level=None)
    c.execute("PRAGMA busy_timeout=10000")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.executescript(_SCHEMA)
    return c


def _windows(text: str) -> list[str]:
    """4-line windows, step 3, >= 60 chars."""
    lines = text.split("\n")
    wins, i = [], 0
    while i < len(lines):
        chunk = "\n".join(lines[i:i + 4])
        if not chunk.strip():
            i += 4
            continue
        wins.append(chunk)
        i += 3
    return [w for w in wins if len(w.strip()) >= 60]


def _chunks_of(txt: str) -> list[tuple[str, str]]:
    marks = list(_PAGE_RE.finditer(txt))
    segs: list[tuple[str, str]] = []
    if marks:
        segs.append(("", txt[:marks[0].start()]))
        for k, m in enumerate(marks):
            end = marks[k + 1].start() if k + 1 < len(marks) else len(txt)
            segs.append((m.group(1), txt[m.end():end]))
    else:
        segs.append(("", txt))
    out = []
    for page, seg in segs:
        seg = seg.strip()
        wins = _windows(seg)
        if not wins and len(seg) >= 30:
            wins = [seg[:1400]]
        out += [(page, w) for w in wins]
    return out


def _vault_files(vault: str) -> dict[str, tuple[float, int]]:
    files = {}
    for sub in ("Notes", "Inbox"):
        d = os.path.join(vault, sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.lower().endswith(".md"):
                fp = os.path.join(d, name)
                st = os.stat(fp)
                files[fp] = (st.st_mtime, st.st_size)
    return files


def _unit(vecs) -> np.ndarray:
    a = np.asarray(vecs, dtype="<f4")
    n = np.linalg.norm(a, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return (a / n).astype("<f4")


def _drop_note(c: sqlite3.Connection, path: str) -> None:
    for rid, text in c.execute("SELECT id,text FROM chunks WHERE path=?",
                               (path,)).fetchall():
        c.execute("INSERT INTO chunks_fts(chunks_fts,rowid,text) VALUES('delete',?,?)",
                  (rid, text))
    c.execute("DELETE FROM chunks WHERE path=?", (path,))
    c.execute("DELETE FROM notes WHERE path=?", (path,))


def ensure(vault: str, budget_s: float | None = None) -> dict:
    vault = os.path.abspath(os.path.expanduser(vault))
    files = _vault_files(vault)
    c = _connect()
    try:
        have = {p: (m, s) for p, m, s in c.execute("SELECT path,mtime,size FROM notes")}
        removed = 0
        for p in set(have) - set(files):
            c.execute("BEGIN IMMEDIATE")
            _drop_note(c, p)
            c.execute("COMMIT")
            removed += 1
        todo = [p for p, ms in files.items() if have.get(p) != ms]
        if not todo:
            return {"indexed": 0, "removed": removed, "pending": 0}
        if not se._ensure_embed_server():
            raise RuntimeError("embedding server unavailable (bge-m3 llama-server)")
        t0, indexed = time.time(), 0
        for p in todo:
            if budget_s is not None and time.time() - t0 > budget_s:
                break
            mtime, size = files[p]
            try:
                chunks = _chunks_of(se._read_note(p))
                vecs = (_unit(se._embed_texts([t[:1400] for _, t in chunks]))
                        if chunks else [])
            except OSError:
                continue
            c.execute("BEGIN IMMEDIATE")
            try:
                _drop_note(c, p)
                for i, ((page, text), v) in enumerate(zip(chunks, vecs)):
                    cur = c.execute(
                        "INSERT INTO chunks(path,page,ord,text,vec) VALUES(?,?,?,?,?)",
                        (p, page, i, text, v.tobytes()))
                    c.execute("INSERT INTO chunks_fts(rowid,text) VALUES(?,?)",
                              (cur.lastrowid, text))
                c.execute("INSERT INTO notes VALUES(?,?,?)", (p, mtime, size))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
            indexed += 1
        return {"indexed": indexed, "removed": removed,
                "pending": len(todo) - indexed}
    finally:
        c.close()


_MAT: dict = {"key": None, "ids": None, "mat": None, "paths": None, "pages": None}


def _matrix(c: sqlite3.Connection):
    key = (db_path(), *c.execute("SELECT COALESCE(MAX(id),0),COUNT(*) FROM chunks").fetchone())
    if _MAT["key"] != key:
        rows = c.execute("SELECT id,path,page,vec FROM chunks ORDER BY id").fetchall()
        _MAT["ids"] = np.array([r[0] for r in rows], dtype=np.int64)
        _MAT["paths"] = np.array([r[1] for r in rows], dtype=object)
        _MAT["pages"] = [r[2] for r in rows]
        _MAT["mat"] = (np.frombuffer(b"".join(r[3] for r in rows), dtype="<f4")
                       .reshape(len(rows), -1) if rows else np.zeros((0, 1), "<f4"))
        _MAT["key"] = key
    return _MAT


def _tokens(text: str) -> list[str]:
    return [t for t in _TOK.findall(text.lower().replace('"', " ")) if t not in STOP]


def _page_ok(pg, pages) -> bool:
    if not pages:
        return True
    try:
        return pages[0] <= int(pg) <= pages[1]
    except (TypeError, ValueError):
        return False


def search(query: str, vault: str, only_file: str | None = None,
           pages: tuple[int, int] | None = None, k: int = 6) -> dict:
    vault = os.path.abspath(os.path.expanduser(vault))
    ensure(vault, budget_s=60)
    if not se._ensure_embed_server():
        raise RuntimeError("embedding server unavailable (bge-m3 llama-server)")
    qv = _unit([se._embed_texts([query])[0]])[0]
    c = _connect()
    try:
        m = _matrix(c)
        ids, paths, pgs, mat = m["ids"], m["paths"], m["pages"], m["mat"]
        if not len(ids):
            return {"mode": "none", "results": []}
        sims = mat @ qv
        mask = np.ones(len(ids), dtype=bool)
        if only_file:
            mask &= paths == only_file
        if pages:
            mask &= np.array([_page_ok(p, pages) for p in pgs])
        cand = np.where(mask & (sims >= COS_MIN))[0]
        cand = cand[np.argsort(-sims[cand])][:CAND]
        top_cos = float(sims[cand[0]]) if len(cand) else 0.0
        row_of = {int(i): n for n, i in enumerate(ids)}
        V = [int(ids[n]) for n in cand if sims[n] >= se.SEM_LOW]

        # BM25 + trust gate
        terms = list(dict.fromkeys(_tokens(query)))
        B: list[int] = []
        texts: dict[int, str] = {}
        if terms:
            n_total = len(ids)
            df = {}
            for t in terms:
                r = c.execute("SELECT doc FROM chunks_vocab WHERE term=?", (t,)).fetchone()
                df[t] = r[0] if r else 0
            if all(df[t] > 0 for t in terms):
                idf = {t: math.log((n_total + 1) / (df[t] + 1)) + 0.1 for t in terms}
                need = TRUST * sum(idf.values())
                match = " OR ".join(f'"{t}"' for t in terms)
                sql = ("SELECT c.id,c.path,c.page,c.text FROM chunks_fts "
                       "JOIN chunks c ON c.id=chunks_fts.rowid WHERE chunks_fts MATCH ?")
                args = [match]
                if only_file:
                    sql += " AND c.path=?"
                    args.append(only_file)
                sql += " ORDER BY bm25(chunks_fts) LIMIT ?"
                args.append(CAND * 10 if pages else CAND)
                for rid, _p, pg, text in c.execute(sql, args):
                    if not _page_ok(pg, pages) or len(B) >= CAND:
                        continue
                    present = set(_TOK.findall(text.lower()))
                    if sum(idf[t] for t in terms if t in present) >= need:
                        B.append(rid)
                        texts[rid] = text

        if top_cos >= se.SEM_STRONG or B:
            mode = "hybrid"
            score: dict[int, float] = {}
            for lst in (V, B):
                for r, rid in enumerate(lst, 1):
                    score[rid] = score.get(rid, 0.0) + 1.0 / (RRF_K + r)
            order = sorted(score, key=lambda i: -score[i])
        elif top_cos >= se.SEM_LOW and len(V) >= LOW_MIN_HITS:
            mode, order = "semantic-low", V
        else:
            return {"mode": "none", "results": []}

        need_txt = [i for i in order if i not in texts]
        for i in need_txt:
            texts[i] = c.execute("SELECT text FROM chunks WHERE id=?", (i,)).fetchone()[0]
        results, per_page, bset = [], {}, set(B)
        for rid in order:
            n = row_of[rid]
            key = (paths[n], pgs[n])
            if per_page.get(key, 0) >= 2:
                continue
            per_page[key] = per_page.get(key, 0) + 1
            results.append({"file": str(paths[n]), "page": pgs[n],
                            "score": round(float(sims[n]), 3),
                            "bm25": rid in bset,
                            "excerpt": " ".join(texts[rid].split())[:900]})
            if len(results) >= k:
                break
        return {"mode": mode, "results": results}
    finally:
        c.close()
