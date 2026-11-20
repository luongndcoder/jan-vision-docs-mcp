#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""webui.py — Local web upload UI for jan-vision-docs-mcp (localhost only).

Binds 127.0.0.1 only — no LAN exposure, no auth. Serves:
  GET  /            — dashboard UI (upload queue + library), localhost only
  POST /upload       — multipart upload (pdf/docx/md/txt/images), queued
  GET  /jobs        — JSON status of every job (pending/running/done/failed)
  GET  /notes       — JSON list of vault notes (Inbox + Notes)
  GET  /note/<name>  — preview a note as raw text (client escapes)
  GET  /download/<name> — download a .md note
  GET  /meta        — {"build": ...} for hot-reload detection
  GET  /healthz     — "ok"

Files are staged in <repo>/uploads/<job-id>/ (NOT _dropbox — the launchd
watcher must not double-process them), then ingested via
server_extra.ingest_document. Originals move to Vault/_done_web/.

Run:  .venv/bin/python webui.py   (or install_webui.sh for launchd)
Env:  PDF_OCR_VAULT (vault root, default ~/Documents/Vault)
      PDF_OCR_WEBUI_PORT (default 8765)
"""
from __future__ import annotations

import html
import shutil
import json
import os
import queue
import threading
import time
import uuid

import jan_sync
from fastapi import Request, FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
import uvicorn

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PDF_OCR_WEBUI_PORT", "8765"))
VAULT = os.path.abspath(os.path.expanduser(
    os.environ.get("PDF_OCR_VAULT", "~/Documents/Vault")))
STAGE = os.path.join(HERE, "uploads")
os.makedirs(STAGE, exist_ok=True)

OCR_LANGS = {  # Vision Live Text codes -> label tiếng Việt
    "vi-VN": "Việt", "en-US": "Anh", "zh-Hans": "Trung giản",
    "zh-Hant": "Trung phồn", "ja-JP": "Nhật", "ko-KR": "Hàn",
    "fr-FR": "Pháp", "de-DE": "Đức", "ru-RU": "Nga",
}

SUPPORTED = {".pdf", ".docx", ".md", ".markdown", ".txt", ".text",
             ".png", ".jpg", ".jpeg", ".heic", ".heif", ".tiff", ".tif",
             ".webp", ".bmp", ".gif"}

app = FastAPI(title="jan-vision-docs web UI", docs_url=None, redoc_url=None)
BUILD = str(int(os.path.getmtime(__file__)))

JOBS: dict[str, dict] = {}
Q: "queue.Queue[str]" = queue.Queue()


def _worker() -> None:
    import sys
    sys.path.insert(0, HERE)
    import server  # noqa - loads server_extra pipeline (Vision, pyobjc)
    se = server.server_extra
    while True:
        jid = Q.get()
        job = JOBS.get(jid)
        if not job:
            continue
        job["status"] = "running"
        job["started"] = time.time()
        t0 = time.time()
        try:
            r = json.loads(se.ingest_document(
                path=job["src"], languages=job.get("langs")) or "{}")
            job["status"] = "done"
            job["note"] = os.path.basename(r.get("written", ""))
            job["seconds"] = round(time.time() - t0, 1)
            # move original next to the vault like the folder watcher does
            done_dir = os.path.join(se.VAULT_DEFAULT, "_done_web")
            try:
                os.makedirs(done_dir, exist_ok=True)
                newp = os.path.join(done_dir, os.path.basename(job["src"]))
                if not os.path.exists(newp):
                    os.replace(job["src"], newp)
            except OSError:
                pass  # staging leftover is not fatal
        except Exception as e:  # noqa BLE001 — surface every failure to UI
            job["status"] = "failed"
            job["error"] = str(e)[:300]


threading.Thread(target=_worker, daemon=True).start()


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/meta")
def meta():
    return {"build": BUILD}


@app.post("/upload")
async def upload(files: list[UploadFile] = File(...),
                 langs: str = Form(None)):
    """langs = comma-separated Vision language codes (vi-VN,en-US,...);
    defaults to PDF_OCR_LANGS. Fewer languages -> faster recognition."""
    use_langs = ",".join([c for c in (langs or "").split(",")
                          if c in OCR_LANGS]) or None
    out = []
    for f in files:
        base = os.path.basename(f.filename or "file")
        ext = os.path.splitext(base)[1].lower()
        if ext not in SUPPORTED:
            out.append({"name": base, "error": f"Định dạng không hỗ trợ: {ext}"})
            continue
        dup = next((j["id"] for j in JOBS.values()
                    if j["name"] == base and j["status"] in ("pending", "running")),
                   None)
        if dup:
            out.append({"id": dup, "name": base, "status": "waiting",
                        "dup": True})
            continue
        # stage inside a per-job subdir so the ORIGINAL stem is preserved
        # (ingest_document derives the note name from the file stem)
        jid = uuid.uuid4().hex[:10]
        job_dir = os.path.join(STAGE, jid)
        os.makedirs(job_dir, exist_ok=True)
        dst = os.path.join(job_dir, base)
        with open(dst, "wb") as fh:
            fh.write(await f.read())
        JOBS[jid] = {"id": jid, "name": base, "src": dst,
                     "status": "pending", "added": time.time(),
                     "langs": use_langs}
        Q.put(jid)
        out.append({"id": jid, "name": base, "status": "pending"})
    return {"jobs": out}


@app.get("/jobs")
def jobs():
    return list(JOBS.values())


@app.get("/notes")
def notes():
    items = []
    for sub in ("Inbox", "Notes"):
        d = os.path.join(VAULT, sub)
        if not os.path.isdir(d):
            continue
        for n in sorted(os.listdir(d)):
            if n.lower().endswith(".md"):
                fp = os.path.join(d, n)
                kb = os.path.getsize(fp) / 1024
                items.append({"name": n, "folder": sub, "kb": round(kb, 1),
                              "mtime": time.strftime(
                                  "%d/%m · %H:%M", time.localtime(
                                      os.path.getmtime(fp)))})
    return items


def _note_path(name: str) -> str:
    base = os.path.basename(name)
    if not base.endswith(".md"):
        raise HTTPException(404, "not a note")
    for sub in ("Inbox", "Notes"):
        fp = os.path.join(VAULT, sub, base)
        if os.path.isfile(fp):
            return fp
    raise HTTPException(404, "note not found")


@app.get("/note/{name}")
def note(name: str):
    fp = _note_path(name)
    txt = open(fp, encoding="utf-8", errors="replace").read()
    return PlainTextResponse(txt, media_type="text/plain; charset=utf-8")


def _to_trash(path: str) -> str:
    """Move a file/folder to ~/.Trash (recoverable in Finder)."""
    base = os.path.basename(path.rstrip("/"))
    dst = os.path.expanduser(f"~/.Trash/{base}")
    i = 1
    while os.path.exists(dst):
        stem, ext = os.path.splitext(base)
        dst = os.path.expanduser(f"~/.Trash/{stem} {i}{ext}")
        i += 1
    shutil.move(path, dst)
    return dst


@app.post("/note-delete")
def note_delete(body: dict):
    """Xoá note khỏi thư viện: chuyển .md (+ ảnh đính kèm) vào Thùng rác và
    gỡ khỏi mọi Jan project để Jan không còn trích dẫn nội dung đã xoá."""
    fp = _note_path(body.get("name") or "")
    removed_from = []
    for p in jan_sync.list_projects():
        try:
            if jan_sync.remove_note(p["db"], fp):
                removed_from.append(p["name"])
        except Exception:  # noqa BLE001 — DB project lỗi không chặn việc xoá note
            pass
    trashed = _to_trash(fp)
    stem = os.path.splitext(os.path.basename(fp))[0]
    att = os.path.join(VAULT, "Attachments", stem)
    if os.path.isdir(att):
        _to_trash(att)
    return {"ok": True, "trashed": trashed, "jan_projects": removed_from}


@app.get("/download/{name}")
def download(name: str):
    fp = _note_path(name)
    return FileResponse(fp, filename=os.path.basename(fp))


def _vault_note_paths() -> list[str]:
    """Absolute paths of every vault note (same order as /notes)."""
    paths = []
    for sub in ("Inbox", "Notes"):
        d = os.path.join(VAULT, sub)
        if not os.path.isdir(d):
            continue
        for n in sorted(os.listdir(d)):
            if n.lower().endswith(".md"):
                paths.append(os.path.join(d, n))
    return paths


def _proj_db(project_id: str) -> str | None:
    for p in jan_sync.list_projects():
        if p["id"] == project_id:
            return p["db"]
    return None


@app.get("/projects")
def projects():
    return jan_sync.list_projects()


@app.get("/jan-embed")
def jan_embed():
    """Jan's project embedding model matches what we write (bge-m3)?"""
    return jan_sync.embed_status()


@app.get("/jan-state")
def jan_state(projects: str = ""):
    """state cho NHIỀU project: projects=id1,id2 -> {pid: {path: {...}}}"""
    projs = {p["id"]: p for p in jan_sync.list_projects()}
    out = {}
    for pid in [p for p in projects.split(",") if p]:
        if pid in projs:
            out[pid] = jan_sync.project_state(projs[pid]["db"],
                                              _vault_note_paths())
    return {"projects": out}


@app.post("/jan-sync")
def jan_sync_ep(body: dict):
    """Sync notes (hoặc cả vault) vào MỘT HAY NHIỀU project."""
    names = body.get("notes") or []
    pids = body.get("projects") or []
    if isinstance(pids, str):
        pids = [p for p in pids.split(",") if p]
    if body.get("project"):
        pids.append(body["project"])
    st = jan_sync.embed_status()
    if not st["ok"]:
        return {"error": st["reason"]}
    projs = {p["id"]: p for p in jan_sync.list_projects()}
    pids = [p for p in dict.fromkeys(pids) if p in projs]
    if not pids:
        return {"error": "chưa chọn project hợp lệ nào"}
    per = {}
    for pid in pids:
        db = projs[pid]["db"]
        if not names:  # all vault notes
            r = jan_sync.sync_all(db, VAULT)
            per[pid] = {"name": projs[pid]["name"], "summary": r}
            continue
        results = []
        for name in names:
            try:
                fp = _note_path(name)
            except HTTPException:
                results.append({"note": name, "ok": False,
                                "error": "không có note"})
                continue
            results.append({"note": name, **jan_sync.sync_note(db, fp)})
        per[pid] = {"name": projs[pid]["name"], "results": results}
    return {"projects": per}


@app.post("/jan-unsync")
def jan_unsync_ep(body: dict):
    """Gỡ note khỏi các Jan project (note vẫn ở lại thư viện/vault)."""
    name = body.get("note") or ""
    fp = _note_path(name)
    pids = set(body.get("projects") or [])
    removed = []
    for p in jan_sync.list_projects():
        if p["id"] in pids and jan_sync.remove_note(p["db"], fp):
            removed.append(p["name"])
    return {"ok": True, "removed_from": removed}


@app.post("/jan-purge")
def jan_purge_ep(body: dict):
    """Xoá file DB của project ĐÃ XOÁ trong Jan (chuyển vào ~/.Trash)."""
    try:
        return {"ok": True, "trashed": jan_sync.purge_project(body.get("id") or "")}
    except ValueError as e:
        return {"error": str(e)}
    except FileNotFoundError as e:
        return {"error": str(e)}


_FAVICON = ("data:image/svg+xml," +
            "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
            "%3Crect width='64' height='64' rx='16' fill='%23e08a5c'/%3E"
            "%3Cpath d='M20 26h9l4 5h11a3 3 0 0 1 3 3v12a3 3 0 0 1-3 3"
            "H21a3 3 0 0 1-3-3V29z' fill='%230c0e11'/%3E"
            "%3Ccircle cx='41' cy='22' r='5' fill='%230c0e11'/%3E%3C/svg%3E")

PAGE = """<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vision Docs</title>
<link rel="icon" href="""" + _FAVICON + """">
<link href="/fonts/fonts.css" rel="stylesheet">
<style>
:root{
  color-scheme:dark;
  --bg:#0c0e11; --panel:#12151a; --panel2:#171b21;
  --line:#242a33; --text:#e8eaee; --muted:#8f99a5; --faint:#5c6672;
  --accent:#e08a5c; --accent-dim:#8a5133; --ok:#4fb071; --err:#d9695a;
  --shadow:0 1px 0 #ffffff08 inset, 0 20px 50px -30px #000000cc;
  --sans:"Outfit",-apple-system,"SF Pro Text",system-ui,sans-serif;
  --mono:"JetBrains Mono","SF Mono",ui-monospace,monospace;
  --line-strong:#333b46;
}
html[data-theme="light"]{
  color-scheme:light;
  --bg:#f7f6f3; --panel:#ffffff; --panel2:#f1efe9;
  --line:#e6e2d9; --text:#201f1c; --muted:#6e6b63; --faint:#a09d92;
  --accent:#c06735; --accent-dim:#8a4a25; --ok:#2e8f57; --err:#bf4a37;
  --shadow:0 1px 0 #ffffff inset, 0 20px 50px -28px #00000024;
  --line-strong:#cfc9bd;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);
  font:400 15px/1.55 var(--sans);
  background-image:radial-gradient(900px 480px at 85% -10%,#e08a5c12,transparent 60%),
                   radial-gradient(700px 420px at -10% 110%,#27413a14,transparent 65%)}
html[data-theme="light"] body::before{filter:invert(1);opacity:.3}
body::before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.5;z-index:0;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2'/%3E%3CfeColorMatrix values='0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 0 0 0 0.04 0'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)'/%3E%3C/svg%3E")}
::selection{background:#e08a5c33}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
.mono{font-family:var(--mono)}

/* ---------- layout: sidebar + main ---------- */
#shell{display:grid;grid-template-columns:236px 1fr;min-height:100dvh}
#shell>aside{position:sticky;top:0;height:100dvh;display:flex;flex-direction:column;
  gap:6px;padding:18px 14px;border-right:1px solid var(--line);
  background:color-mix(in srgb,var(--bg) 80%,transparent);backdrop-filter:blur(10px);z-index:4}
.brand{display:flex;align-items:center;gap:10px;font-weight:600;font-size:15.5px;
  letter-spacing:-.01em;padding:2px 8px 14px}
.mark{width:30px;height:30px;border-radius:9px;background:linear-gradient(145deg,#e89a6d,#c8704a);
  display:grid;place-items:center;flex:none;
  box-shadow:0 4px 14px -4px #e08a5c55,0 0 0 1px #ffffff14 inset}
.mark svg{width:16px;height:16px}
.navlbl{font-size:10.5px;font-weight:600;letter-spacing:.1em;color:var(--faint);
  text-transform:uppercase;padding:10px 10px 4px}
.nav{display:flex;align-items:center;gap:11px;width:100%;text-align:left;
  padding:10px 12px;border:0;border-radius:10px;background:none;
  color:var(--muted);font:500 14px var(--sans);cursor:pointer;
  transition:background .18s,color .18s}
.nav svg{flex:none;opacity:.75}
.nav:hover{background:#ffffff06;color:var(--text)}
.nav:active{transform:translateY(1px)}
.nav.on{background:var(--panel2);color:var(--text);
  box-shadow:0 0 0 1px var(--line),0 1px 0 #ffffff08 inset}
.nav.on svg{opacity:1;color:var(--accent)}
.nbadge{margin-left:auto;font:500 11px var(--mono);color:var(--faint);
  background:#ffffff08;padding:3px 8px;border-radius:6px}
.nav.on .nbadge{color:var(--accent)}
.sidefoot{margin-top:auto;display:flex;flex-direction:column;gap:10px;padding:12px 10px 2px;
  border-top:1px solid var(--line)}
.vault{font:400 10.5px/1.5 var(--mono);color:var(--faint);overflow-wrap:anywhere}
.srv{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ok);flex:none;
  box-shadow:0 0 8px #4fb07188;animation:breathe 2.4s ease-in-out infinite}
@keyframes breathe{50%{opacity:.55}}

main{padding:36px 40px 90px;position:relative;z-index:1}
.view{display:none;max-width:860px;margin:0 auto}
.view.on{display:block;animation:fadein .3s ease both}
@keyframes fadein{from{opacity:0;transform:translateY(8px)}}
.vhead{margin:2px 0 20px}
.vhead h1{font-size:clamp(23px,3.2vw,30px);font-weight:600;letter-spacing:-.025em;
  margin:0;line-height:1.15;text-wrap:balance}
.vhead p{margin:7px 0 0;color:var(--muted);font-size:14px;max-width:58ch}
.vrow{display:flex;justify-content:space-between;align-items:end;gap:16px;flex-wrap:wrap}

/* ---------- dropzone ---------- */
#drop{position:relative;cursor:pointer;overflow:hidden;
  border:1px solid var(--line);border-radius:16px;background:var(--panel);
  padding:26px 22px;display:flex;align-items:center;gap:18px;text-align:left;
  transition:border-color .25s,transform .15s;--mx:50%;--my:50%}
#drop::before{content:"";position:absolute;inset:0;opacity:0;transition:opacity .3s;
  background:radial-gradient(300px 180px at var(--mx) var(--my),#e08a5c14,transparent 70%)}
#drop:hover,#drop.over{border-color:var(--accent-dim)}
#drop.over{transform:scale(.995);border-color:var(--accent)}
#drop:hover::before,#drop.over::before{opacity:1}
#drop .tz{width:46px;height:46px;border-radius:13px;flex:none;
  background:var(--panel2);border:1px solid var(--line);display:grid;place-items:center;transition:transform .25s cubic-bezier(.2,.9,.3,1.2)}
#drop:hover .tz,#drop.over .tz{transform:translateY(-3px)}
#drop b{font-weight:600;font-size:15.5px;letter-spacing:-.01em}
#drop small{display:block;margin-top:3px;color:var(--muted);font-size:12.5px}
.kinds{display:flex;gap:5px;margin-top:10px;flex-wrap:wrap}
.kind{font:500 10.5px var(--mono);color:var(--faint);padding:4px 8px;
  border:1px solid var(--line);border-radius:6px;background:var(--bg)}
#drop:hover .kind,#drop.over .kind{color:var(--accent);border-color:#e08a5c38}

/* ---------- queue panels ---------- */
.panel{margin-top:22px;border:1px solid var(--line);border-radius:14px;
  background:var(--panel);overflow:hidden}
.phead{display:flex;align-items:center;gap:10px;padding:13px 16px;
  border-bottom:1px solid var(--line);background:var(--panel2)}
.phead h2{font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);margin:0}
.qstat{margin-left:auto;font:500 11.5px var(--mono);color:var(--faint)}
.qstat b{color:var(--text);font-weight:600}
.jlist{display:grid}
.jlist:empty{display:none}

.job{display:grid;grid-template-columns:22px 1fr auto;gap:13px;align-items:center;
  padding:12px 16px;border-bottom:1px solid #1c212a;transition:background .15s}
.job:last-child{border-bottom:0}
.job:hover{background:#ffffff04}
.jstatus{width:20px;height:20px;border-radius:6px;display:grid;place-items:center;
  font-size:12px;background:var(--panel2);border:1px solid var(--line)}
.jname{font-weight:500;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.jinfo{font:400 11.5px var(--mono);color:var(--faint);margin-top:2px}
.jact{font-size:13px;display:inline-flex;align-items:center;gap:8px;white-space:nowrap}
.pend{color:var(--muted)}
.ok{color:var(--ok)}.fail{color:var(--err)}
.spin{width:12px;height:12px;border-radius:50%;flex:none;
  border:1.6px solid #ffffff18;border-top-color:var(--accent);
  animation:r .8s linear infinite}
@keyframes r{to{transform:rotate(360deg)}}
.jtime{font:500 12px var(--mono);color:var(--muted);font-variant-numeric:tabular-nums}
.jact a{color:var(--accent);text-decoration:none;font:500 12.5px var(--mono);
  padding:5px 10px;border:1px solid #e08a5c33;border-radius:7px;
  transition:background .2s,transform .1s}
.jact a:hover{background:#e08a5c14}
.jact a:active{transform:translateY(1px)}
.job.pop{animation:pop .4s ease}
@keyframes pop{30%{background:#e08a5c0f}}
.empty-q{padding:26px 20px;text-align:center;color:var(--faint);font-size:13px}

/* ---------- library ---------- */
.chips{display:flex;gap:7px;align-items:center;flex-wrap:wrap}
.nq{font:400 13px var(--sans);color:var(--text);background:var(--panel);
  border:1px solid var(--line);border-radius:10px;padding:7px 12px;width:200px;outline:none;height:34px}
.nq:focus{border-color:var(--line-strong)}
.seg{display:inline-flex;gap:2px;padding:3px;border-radius:10px;
  background:var(--panel2);border:1px solid var(--line)}
.chip{font:500 12.5px var(--sans);color:var(--muted);cursor:pointer;
  background:none;border:0;padding:5px 12px;border-radius:7px;
  display:inline-flex;align-items:center;gap:6px;
  transition:color .18s,background .18s,box-shadow .18s}
.chip:hover{color:var(--text)}
.chip.on{color:var(--text);background:var(--panel);
  box-shadow:0 1px 2px #0000001a,0 0 0 1px var(--line)}
.cnt{font:500 10.5px var(--mono);color:var(--faint)}
.cnt:empty{display:none}
#notes{margin-top:16px;display:grid;gap:8px}
.nrow{display:grid;grid-template-columns:auto minmax(0,1fr) auto auto;gap:14px;align-items:center;
  padding:13px 16px;border-radius:12px;background:var(--panel);
  border:1px solid transparent;transition:border-color .2s;
  animation:enter2 .4s ease both}
@keyframes enter2{from{opacity:0;transform:translateY(9px)}}
.nrow:hover{border-color:var(--line)}
.ftag{font:500 10.5px var(--mono);color:var(--faint);padding:4px 8px;
  border:1px solid var(--line);border-radius:6px}
.nname{font-weight:500;font-size:14.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nname a{text-decoration:none;cursor:pointer}
.nname a:hover{color:var(--accent)}
.ninfo{font:400 12px var(--mono);color:var(--muted);text-align:right;
  font-variant-numeric:tabular-nums}
.acts{display:flex;gap:4px;justify-content:flex-end;align-items:center}
.acts .ib{width:32px;height:32px;padding:0;justify-content:center;border-color:transparent;
  border-radius:8px;color:var(--faint)}
.acts .ib:hover{background:var(--panel2);border-color:var(--line);color:var(--text)}
.acts .ib svg{width:15px;height:15px}
.acts .ib.del:hover{color:var(--err);border-color:color-mix(in srgb,var(--err) 35%,transparent);
 background:color-mix(in srgb,var(--err) 8%,transparent)}
.acts .ib.del.arm{width:auto;padding:0 10px;color:#fff;background:var(--err);border-color:var(--err);
 font:600 12px var(--sans)}
.acts .ib.del.arm svg{display:none}
.acts .ib.del.busy{opacity:.5;pointer-events:none}
.acts a,.acts button,.btn2,.jb{display:inline-flex;align-items:center;gap:6px}
.acts a,.acts button{font:500 13px var(--sans);color:var(--muted);cursor:pointer;
  text-decoration:none;background:none;border:1px solid var(--line);
  padding:6px 12px;border-radius:8px;transition:color .2s,border-color .2s,transform .1s}
.acts a:hover,.acts button:hover{color:var(--text);border-color:var(--line-strong)}
.acts a:active,.acts button:active{transform:translateY(1px)}
.empty{padding:52px 20px;text-align:center;color:var(--muted);
  border:1px dashed var(--line);border-radius:14px}
.empty svg{opacity:.35;margin-bottom:10px}

/* ---------- panel preview (slide-over) ---------- */
#panel{position:fixed;top:0;right:0;bottom:0;width:min(560px,94vw);z-index:20;
  background:var(--panel);border-left:1px solid var(--line);
  transform:translateX(102%);transition:transform .28s cubic-bezier(.2,.8,.25,1);
  display:flex;flex-direction:column;box-shadow:none;visibility:hidden;
  transition:transform .28s cubic-bezier(.2,.8,.25,1),visibility 0s .28s}
#panel.open{transform:none;visibility:visible;transition-delay:0s;
  box-shadow:-30px 0 70px -40px #000000d9}
.pheadp{display:flex;align-items:center;gap:12px;padding:15px 20px;
  border-bottom:1px solid var(--line)}
.pheadp .t{font-weight:600;font-size:14.5px;flex:1;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.pbody{flex:1;overflow:auto;padding:18px 20px;margin:0}
.pbody{font:400 13.5px/1.8 var(--sans);color:var(--text)}
#pview{overflow-wrap:anywhere}
#praw{white-space:pre-wrap;overflow-wrap:anywhere}
.pbody p{margin:0 0 13px}
.pbody h2{font:700 17px/1.4 var(--sans);margin:20px 0 10px;padding-bottom:6px;
  border-bottom:1px solid var(--line)}
.pbody h3{font:650 15px/1.4 var(--sans);margin:16px 0 8px}
.pbody h4,.pbody h5{font:600 14px/1.4 var(--sans);margin:14px 0 8px}
.pbody hr{border:0;border-top:1px solid var(--line);margin:18px 0}
.pbody ul,.pbody ol{margin:0 0 13px;padding-left:22px}
.pbody li{margin:3px 0}
.pbody code{font:400 12px var(--mono);background:var(--panel2);
  border:1px solid var(--line);border-radius:5px;padding:1px 5px}
.pmeta{font:400 11.5px/1.75 var(--mono);background:var(--panel2);
  border:1px solid var(--line);border-radius:10px;padding:10px 13px;
  color:var(--muted);margin:0 0 16px}
.pmeta b{color:var(--accent);font-weight:600}
.pbody pre{white-space:pre-wrap;word-break:break-word;margin:0;
  font:400 12.5px/1.7 var(--mono);color:var(--text)}
.pmodes{display:inline-flex;border:1px solid var(--line);border-radius:9px;
  overflow:hidden;flex:none}
.pmodes button{font:500 11.5px var(--sans);color:var(--muted);background:transparent;
  border:0;padding:6px 11px;cursor:pointer;transition:color .15s}
.pmodes button.on{background:var(--panel2);color:var(--accent)}
#scrim{position:fixed;inset:0;background:#00000066;opacity:0;pointer-events:none;
  transition:opacity .25s;z-index:15}
#scrim.on{opacity:1;pointer-events:auto}
.xbtn{display:inline-flex;align-items:center;justify-content:center;
  background:var(--panel2);border:1px solid var(--line);color:var(--muted);
  width:30px;height:30px;border-radius:8px;cursor:pointer;font-size:15px;flex:none;
  transition:color .2s,border-color .2s}
.xbtn:hover{color:var(--text)}
.xbtn:active{transform:translateY(1px)}
.janwarn{flex-basis:100%;margin-top:8px;padding:9px 12px;border-radius:9px;
  border:1px solid color-mix(in srgb,var(--err) 45%,transparent);
  background:color-mix(in srgb,var(--err) 10%,transparent);color:var(--err);
  font-size:13px;line-height:1.45}
.toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,80px);
  background:var(--panel2);border:1px solid #d9695a44;color:var(--err);
  padding:11px 18px;border-radius:11px;font-size:13.5px;z-index:30;
  transition:transform .3s cubic-bezier(.2,.8,.25,1);box-shadow:var(--shadow)}
.toast.on{transform:translate(-50%,0)}

/* ---------- sticky header thư viện + load more ---------- */
.stickybar{position:sticky;top:0;z-index:9;margin:-14px 0 6px;padding:14px 0 2px;
 background:var(--bg)}
.stickybar::after{content:"";position:absolute;left:0;right:0;top:100%;height:14px;
 pointer-events:none;background:linear-gradient(to bottom,var(--bg),transparent)}
.lmore{display:flex;justify-content:center;padding:6px 0 2px}

/* ---------- custom project dropdown ---------- */
.projdd{position:relative}
.projbtn{display:inline-flex;align-items:center;gap:9px;
 font:500 13px var(--sans);color:var(--text);cursor:pointer;background:var(--panel2);
 border:1px solid var(--line);padding:7px 12px;border-radius:9px;
 transition:border-color .18s,transform .1s}
.projbtn:hover{border-color:var(--line-strong)}
.projbtn:active{transform:translateY(1px)}
.projbtn svg{opacity:.55;transition:transform .2s}
.projdd.open .projbtn svg{transform:rotate(180deg)}
.projdd.open .projbtn{border-color:var(--accent-dim)}
.projpop{position:absolute;top:calc(100% + 6px);left:0;min-width:270px;z-index:12;
 background:var(--panel2);border:1px solid var(--line);border-radius:12px;
 padding:6px;box-shadow:0 24px 60px -18px #000000e6,0 1px 0 #ffffff08 inset;
 opacity:0;transform:translateY(-6px);pointer-events:none;
 transition:opacity .16s,transform .16s}
.projdd.open .projpop{opacity:1;transform:none;pointer-events:auto}
.prow{display:flex;align-items:center;gap:10px;padding:8px 9px;border-radius:8px;
 cursor:pointer;color:var(--muted);transition:background .13s,color .13s}
.prow:hover{background:color-mix(in srgb,var(--text) 5%,transparent);color:var(--text)}
.pchk{width:17px;height:17px;border-radius:5px;border:1px solid var(--line);
 display:grid;place-items:center;font-size:11px;flex:none;color:transparent;
 transition:background .14s,border-color .14s,color .14s}
.prow.on .pchk{background:var(--accent-dim);border-color:var(--accent);color:#fff}
.prow.on .pname{color:var(--text)}
.pname{font-weight:500;font-size:13.5px}
.pjmeta{margin-left:auto;padding-left:14px;font:400 11px var(--mono);color:var(--faint);white-space:nowrap}
.pempty{padding:11px 12px;color:var(--faint);font-size:13px}

.psep{height:1px;background:var(--line);margin:6px 4px}
.plabel{font:600 10px var(--sans);letter-spacing:.1em;text-transform:uppercase;
 color:var(--faint);padding:7px 9px 4px}
.prow.dead{cursor:default;opacity:.75}
.prow.dead:hover{background:none;color:var(--muted)}
.prow.dead .pname{text-decoration:line-through;text-decoration-color:var(--faint)}
.prow.dead .pchk{border-style:dashed;border-color:var(--line)}
.ppurge{margin-left:10px;font:500 11px var(--sans);color:var(--err);cursor:pointer;
 background:none;border:1px solid var(--err);padding:4px 9px;border-radius:7px;
 transition:background .15s,transform .1s;white-space:nowrap}
.ppurge:hover{background:#d9695a1a}
.ppurge:active{transform:translateY(1px)}
.ppurge.armed{background:var(--err);color:#fff}
.pshort{font:400 10px var(--mono);color:var(--faint)}

/* ---------- lang chips ---------- */
.langbar{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-top:12px}
.langbar .ll{font-size:12.5px;color:var(--muted);margin-right:2px}
.lchip{font:500 12px var(--sans);color:var(--faint);cursor:pointer;user-select:none;
 background:none;border:1px solid var(--line);padding:5px 11px;border-radius:999px;
 transition:color .18s,border-color .18s,background .18s}
.lchip:hover{color:var(--text)}
.lchip.on{color:var(--text);border-color:#e08a5c55;background:#e08a5c12}
.langbar small{color:var(--faint);font-size:11.5px;margin-left:auto}

/* ---------- jan sync ---------- */
.projbar{display:flex;gap:10px;align-items:center;margin:2px 0 16px;flex-wrap:wrap}
.projbar label{color:var(--muted);font-size:13px}
#proj{background:var(--panel);border:1px solid var(--line);color:var(--text);
 font:500 13px var(--sans);padding:7px 10px;border-radius:9px;cursor:pointer}
#proj:focus-visible{outline-offset:0}
.btn2{font:500 13px var(--sans);color:var(--muted);cursor:pointer;background:none;
 border:1px solid var(--line);padding:7px 13px;border-radius:9px;
 transition:color .2s,border-color .2s,transform .1s}
.btn2:hover{color:var(--text);border-color:var(--line-strong)}
.btn2:active{transform:translateY(1px)}
.btn2.busy{opacity:.4;pointer-events:none}
.acts .jb{display:inline-flex;align-items:center;gap:7px;margin-right:6px;
 font:500 12.5px var(--sans);color:var(--muted);border:1px dashed var(--line-strong);
 padding:5px 11px;border-radius:999px;cursor:pointer;text-decoration:none;
 transition:color .18s,border-color .18s,background .18s;white-space:nowrap}
.acts .jb:hover{color:var(--text);border-style:solid}
.jb .jd{width:7px;height:7px;border-radius:50%;background:currentColor;opacity:.55}
.acts .jb.on{color:var(--ok);border:1px solid transparent;
 background:color-mix(in srgb,var(--ok) 11%,transparent)}
.jb.on .jd{opacity:1}
.acts .jb.arm{color:#fff;background:var(--err);border:1px solid var(--err)}
.acts .jb.part{color:var(--accent);border:1px solid transparent;
 background:color-mix(in srgb,var(--accent) 11%,transparent)}
.acts .jb.busy{pointer-events:none;color:var(--accent);border:1px solid transparent;
 background:color-mix(in srgb,var(--accent) 9%,transparent)}
.jspin{width:10px;height:10px;flex:none;border-radius:50%;border:1.6px solid currentColor;
 border-right-color:transparent;animation:jspin .7s linear infinite}
@keyframes jspin{to{transform:rotate(360deg)}}
.btn2.busy .jspin{margin-right:7px;vertical-align:-1px;display:inline-block}
.jb.okpop{animation:pop .4s ease}
.toast.ok{border-color:#4fb07144;color:var(--ok)}
/* icon theme được JS swap trong nút #themebtn */
[data-theme="light"] .toast{box-shadow:0 12px 30px -14px #00000040}
[data-theme="light"] .projpop{box-shadow:0 24px 60px -18px #00000030,0 1px 0 #ffffff60 inset}
[data-theme="light"] ::selection{background:#c0673533}

@media (max-width:860px){
  #shell{grid-template-columns:minmax(0,1fr)}
  #shell>aside{overflow-x:auto;scrollbar-width:none}
  #shell>aside .nav{padding:8px 10px;gap:7px}
  #shell>aside{position:static;height:auto;flex-direction:row;align-items:center;
    padding:12px 16px;border-right:0;border-bottom:1px solid var(--line);gap:4px}
  .brand{padding:0 14px 0 0}
  .navlbl,.sidefoot .vault{display:none}
  .nav{width:auto;padding:9px 12px}
  .sidefoot{margin:0 0 0 auto;border:0;padding:0;flex-direction:row}
  main{padding:24px 16px 70px}
  .brand{font-size:0;gap:0}            /* chỉ giữ logo */
  .nav{white-space:nowrap}
  .srvt{display:none}
  .nrow{grid-template-columns:auto minmax(0,1fr);row-gap:8px;padding:12px}
  .ninfo{grid-column:1/-1;text-align:left}
  .acts{grid-column:1/-1;justify-content:flex-start;flex-wrap:wrap}
  .chips{width:100%}
  .nq{flex:1 1 100%;width:auto}
  #janinfo{flex-basis:100%}
}
</style></head><body>
<div id="jsbadge" style="position:fixed;bottom:8px;right:10px;z-index:50;font:600 10.5px/1 var(--sans);color:#8f99a5;padding:6px 9px;border-radius:8px;background:var(--panel);border:1px solid var(--line);opacity:0;transition:opacity .4s">JS</div>
<script>
/* áp theme trước khi vẽ — mặc định SÁNG nếu chưa từng chọn */
document.documentElement.dataset.theme=
 (localStorage.getItem('vd.theme')||'light');
</script>
<div id="shell">
<aside>
  <div class="brand"><span class="mark"><svg viewBox="0 0 24 24" fill="none">
    <path d="M3 8.5A2.5 2.5 0 0 1 5.5 6h3l2 2.5h8A2.5 2.5 0 0 1 21 11v6a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17z" fill="#0c0e11"/>
    <circle cx="17.5" cy="6.8" r="2.2" fill="#0c0e11"/></svg></span>
    Vision Docs</div>
  <div class="navlbl">Menu</div>
  <button class="nav on" id="nav-up" data-v="upload">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V5m0 0-4 4m4-4 4 4"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>
    Nạp tài liệu</button>
  <button class="nav" id="nav-lib" data-v="library">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><path d="M9 13h6"/></svg>
    Thư viện <span class="nbadge" id="navCount">—</span></button>
  <div class="sidefoot">
    <div style="display:flex;align-items:center;gap:10px">
      <span class="srv" title="server đang chạy"><span class="dot"></span> <span class="srvt">server đang chạy</span></span>
      <button class="xbtn" id="themebtn" aria-label="Chuyển sáng/tối"
        title="Sáng / tối" style="width:26px;height:26px"></button>
    </div>
    <div class="vault" title=\"""" + html.escape(VAULT) + """">""" + html.escape(VAULT.replace(os.path.expanduser("~"), "~", 1)) + """</div>
  </div>
</aside>

<main>
  <section class="view on" id="view-upload">
    <div class="vhead vrow">
      <div><h1>Nạp tài liệu</h1>
        <p>OCR bằng Vision ngay trên máy — không gửi đi đâu cả. Xong tự thành note cho Jan.</p></div>
    </div>

    <div id="drop" tabindex="0" role="button" aria-label="Chọn hoặc thả file">
      <div class="tz"><svg width="22" height="22" viewBox="0 0 24 24" fill="none"
        stroke="#e08a5c" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 15V4m0 0L8 8m4-4 4 4"/><path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/></svg></div>
      <div style="min-width:0">
        <b>Thả file vào đây, hoặc bấm để chọn</b>
        <small>PDF scan, DOCX, ảnh chụp giấy tờ… đều đọc được</small>
        <div class="kinds"><span class="kind">PDF</span><span class="kind">DOCX</span>
          <span class="kind">MD</span><span class="kind">TXT</span><span class="kind">PNG · JPG</span>
          <span class="kind">HEIC</span><span class="kind">TIFF</span></div>
        <div class="langbar" id="langbar" title="Ngôn ngữ OCR cho lần nạp này">
          <span class="ll">Ngôn ngữ</span>
          <button type="button" class="lchip on" data-l="vi-VN">Việt</button>
          <button type="button" class="lchip on" data-l="en-US">Anh</button>
          <button type="button" class="lchip" data-l="zh-Hans">Trung giản</button>
          <button type="button" class="lchip" data-l="zh-Hant">Trung phồn</button>
          <button type="button" class="lchip" data-l="ja-JP">Nhật</button>
          <button type="button" class="lchip" data-l="ko-KR">Hàn</button>
          <button type="button" class="lchip" data-l="fr-FR">Pháp</button>
          <button type="button" class="lchip" data-l="de-DE">Đức</button>
          <button type="button" class="lchip" data-l="ru-RU">Nga</button>
          <small>chọn đúng ngôn ngữ để OCR chính xác</small>
        </div>
      </div>
      <input id="file" type="file" multiple hidden
         accept=".pdf,.docx,.md,.markdown,.txt,.text,.png,.jpg,.jpeg,.heic,.tiff,.webp,.bmp">
    </div>

    <div class="panel">
      <div class="phead"><h2>Hàng đợi</h2><span class="qstat" id="qstat"></span></div>
      <div class="jlist" id="qactive" aria-live="polite"></div>
      <div class="empty-q" id="qempty">Chưa có file nào — thả lên khung phía trên để bắt đầu</div>
    </div>

    <div class="panel" id="pdone" hidden>
      <div class="phead"><h2>Đã xử lý</h2><span class="qstat" id="qdonestat"></span></div>
      <div class="jlist" id="qdone"></div>
    </div>
  </section>

  <section class="view" id="view-library">
    <div class="stickybar">
    <div class="vhead vrow">
      <div><h1>Thư viện</h1><p id="libsub">—</p></div>
      <div class="chips">
        <input type="search" id="nq" class="nq" placeholder="Tìm note…" aria-label="Tìm note theo tên" autocomplete="off">
        <div class="seg" role="tablist" aria-label="Lọc theo thư mục">
          <button class="chip on" data-f="all" role="tab">Tất cả <span class="cnt" data-c="all"></span></button>
          <button class="chip" data-f="Inbox" role="tab">Inbox <span class="cnt" data-c="Inbox"></span></button>
          <button class="chip" data-f="Notes" role="tab">Notes <span class="cnt" data-c="Notes"></span></button>
        </div>
      </div>
    </div>
    <div class="projbar" id="projwrap">
      <label>Jan project</label>
      <div class="projdd" id="projdd">
        <button type="button" class="projbtn" id="projbtn" aria-haspopup="listbox">
          <span id="projbtn-t">Chọn project…</span>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="2.4" stroke-linecap="round"><path d="m6 9 6 6 6-6"/></svg>
        </button>
        <div class="projpop" id="projpop" role="listbox"></div>
      </div>
      <button class="btn2" id="syncall">Đồng bộ tất cả</button>
      <span class="qstat" id="janinfo"></span>
      <div class="janwarn" id="janwarn" hidden></div>
    </div>
    </div>
    <div id="notes"></div>
  </section>
</main>
</div>

<div id="scrim"></div>
<aside id="panel" aria-label="Xem trước note">
  <div class="pheadp"><span class="t" id="ptitle">—</span>
    <span class="pmodes"><button data-pm="view" class="on">Xem trước</button>
      <button data-pm="raw">Nguồn</button></span>
    <a id="pdl" href="#" style="font:500 13px var(--sans);color:var(--accent);
      text-decoration:none;padding:7px 12px;border:1px solid #e08a5c33;border-radius:8px">Tải .md</a>
    <button class="xbtn" id="px" aria-label="Đóng">✕</button></div>
  <div class="pbody"><div id="pview"></div><pre id="praw" hidden></pre></div>
</aside>
<div class="toast" id="toast"></div>

<script>
window.__vderr=function(e){try{var b=document.getElementById('jsbadge');
 if(b){b.style.opacity='1';b.textContent='JS lỗi: '+(e&&e.message||e);
  b.style.color='#e05d4d';}
 document.title='⚠ '+document.title.replace(/^⚠ /,'');}catch(_){}};
window.addEventListener('error',function(e){window.__vderr(e.error||e.message)});
window.addEventListener('unhandledrejection',function(e){window.__vderr(e.reason)});
</script>
<script>

const BUILD="__BUILD__";
const $=s=>document.querySelector(s);
function fjson(url,opts={},ms=6000){
 const c=new AbortController();const t=setTimeout(()=>c.abort(),ms);
 return fetch(url,{...opts,signal:c.signal}).finally(()=>clearTimeout(t));}

/* ---------- menu ---------- */
const views={upload:$('#view-upload'),library:$('#view-library')};
function showView(v){
 for(const k in views)views[k].classList.toggle('on',k===v);
 $('#nav-up').classList.toggle('on',v==='upload');
 $('#nav-lib').classList.toggle('on',v==='library');
 window.scrollTo({top:0,behavior:'smooth'});}
$('#nav-up').onclick=()=>showView('upload');
$('#nav-lib').onclick=()=>showView('library');

/* ---------- theme sáng/tối ---------- */
const SVGS={
 sun:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
 moon:'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>'};
function paintThemeBtn(){
 const dark=document.documentElement.dataset.theme==='dark';
 $('#themebtn').innerHTML=dark?SVGS.sun:SVGS.moon;}
$('#themebtn').onclick=()=>{
 const cur=document.documentElement.dataset.theme==='dark'?'dark':'light';
 const next=cur==='dark'?'light':'dark';
 document.documentElement.dataset.theme=next;
 localStorage.setItem('vd.theme',next);
 paintThemeBtn();};

/* ---------- ngôn ngữ OCR ---------- */
let selLangs=new Set();
function restoreLangs(){
 try{selLangs=new Set(JSON.parse(localStorage.getItem('vd.langs')||''))}catch(e){selLangs=new Set()}
 if(!selLangs.size)selLangs=new Set(['vi-VN','en-US']);
 document.querySelectorAll('.lchip').forEach(c=>
  c.classList.toggle('on',selLangs.has(c.dataset.l)));}
document.querySelectorAll('.lchip').forEach(c=>c.onclick=e=>{
 e.stopPropagation();              /* không mở file dialog khi bấm chip */
 const l=c.dataset.l;
 if(selLangs.has(l))selLangs.delete(l);else selLangs.add(l);
 if(!selLangs.size)selLangs.add(l); /* luôn giữ ít nhất 1 ngôn ngữ */
 c.classList.toggle('on',selLangs.has(l));
 localStorage.setItem('vd.langs',JSON.stringify([...selLangs]));});

/* ---------- dropzone ---------- */
const drop=$('#drop'),fi=$('#file');
const qActive=$('#qactive'),qDone=$('#qdone'),qEmpty=$('#qempty'),
      pDone=$('#pdone'),qStat=$('#qstat'),qDoneStat=$('#qdonestat');
drop.onclick=()=>fi.click();
drop.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();fi.click();}};
['dragover','dragleave','drop'].forEach(ev=>drop.addEventListener(ev,e=>{
 e.preventDefault();drop.classList.toggle('over',ev==='dragover');
 if(ev==='drop')send(e.dataTransfer.files)}));
drop.addEventListener('mousemove',e=>{const r=drop.getBoundingClientRect();
 drop.style.setProperty('--mx',(e.clientX-r.left)+'px');
 drop.style.setProperty('--my',(e.clientY-r.top)+'px');});
fi.onchange=()=>send(fi.files);

function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;')
 .replace(/"/g,'&quot;').replace(/'/g,'&#39;')}

async function send(fs){
 if(!fs||!fs.length)return;
 const fd=new FormData();[...fs].forEach(f=>fd.append('files',f));
 fd.append('langs',[...selLangs].join(','));
 drop.classList.remove('over');
 let r;try{r=await(await fjson('/upload',{method:'POST',body:fd})).json();}
 catch(e){toast('Không tải lên được — server có chạy không?');return;}
 r.jobs.forEach(j=>{
  if(j.dup){return;}
  if(j.error){
   const d=document.createElement('div');d.className='job';d.dataset.err='1';
   d.innerHTML=`<span class="jstatus" style="color:var(--err)">⚠</span>
   <div><div class="jname">${j.name}</div></div>
   <span class="jact fail">${j.error}</span>`;qActive.prepend(d);return;}
  if(!document.getElementById('j_'+j.id))addCard(j.id,j.name);});
 const allDup=r.jobs.length&&r.jobs.every(j=>j.dup);
 if(allDup)toast('File này đang được OCR rồi — xem ở Hàng đợi.');
 fi.value='';}

/* ---------- job cards (change-detection + nhóm active/done) ---------- */
const cards=new Map();   // id -> {el,status}
const lastJobs=new Map();// id -> job server gần nhất (cho elapsed)
function addCard(id,name,langs){
 const lg=langs||[...selLangs].join(',');
 const d=document.createElement('div');d.className='job';d.id='j_'+id;
 d.innerHTML=`<span class="jstatus pend" id="s_${id}">•</span>
  <div style="min-width:0"><div class="jname">${esc(name)}</div>
  <div class="jinfo" id="i_${id}">đang chờ trong hàng</div></div>
  <span class="jact pend"><span class="jtime">${lg}</span></span>`;
 qActive.prepend(d);
 cards.set(id,{el:d,status:'pending'});
 qEmpty.style.display='none';}

function apply(j){
 const id=j.id;let c=cards.get(id);
 const active=j.status==='pending'||j.status==='running';
 if(!c){
  if(active){addCard(id,j.name,j.langs);c=cards.get(id);}
  else if(j.status==='done'||j.status==='failed')c=makeDone(j);
  if(!c)return;}
 if(c.status===j.status)return;
 c.status=j.status;
 const st=document.getElementById('s_'+id),tm=document.getElementById('t_'+id),
       inf=document.getElementById('i_'+id);
 if(j.status==='pending'){st.textContent='•';st.className='jstatus pend';
  tm.className='jact pend';tm.innerHTML='<span class="jtime">—</span>';
  inf.textContent='đang chờ trong hàng';}
 if(j.status==='running'){st.innerHTML='<span class="spin"></span>';st.className='jstatus';
  tm.className='jact';tm.innerHTML='<span class="jtime pend" id="e_'+id+'">0s</span>';
  inf.textContent='đang OCR…';
  c.started=(j.started||j.added)*1000;setBar(c);}
 if(j.status==='done'){setDone(c,j);}
 if(j.status==='failed'){setFailed(c,j);}}
function setDone(c,j){
 const id=j.id;
 const st=document.getElementById('s_'+id);
 st.textContent='✓';st.className='jstatus ok';
 c.el.querySelector('.jact').outerHTML=
  `<span class="jact ok">${j.seconds}s&nbsp;<a href="#" data-note="${esc(j.note)}">xem note</a></span>`;
 c.el.querySelector('.jinfo').textContent='→ '+j.note;
 moveToDone(c);loadNotes();autoJan(j.note);}

function setFailed(c,j){
 const id=j.id,st=document.getElementById('s_'+id);
 st.textContent='✕';st.className='jstatus fail';
 c.el.querySelector('.jact').outerHTML=
  `<span class="jact fail">${j.error||'lỗi'}</span>`;
 c.el.querySelector('.jinfo').textContent='';
 moveToDone(c);}

function moveToDone(c){
 qDone.prepend(c.el);
 c.el.classList.remove('pop');void c.el.offsetWidth;c.el.classList.add('pop');
 pDone.hidden=false;
 const n=qDone.children.length;
 qDoneStat.innerHTML=`<b>${n}</b> file`;
 while(qDone.children.length>25)qDone.lastChild.remove();}

function makeDone(j){ // job đã xong trước khi trang mở — dựng card thẳng vào "Đã xử lý"
 const d=document.createElement('div');d.className='job';d.id='j_'+j.id;
 d.innerHTML=(j.status==='done')
  ?`<span class="jstatus ok">✓</span>
    <div style="min-width:0"><div class="jname">${esc(j.name)}</div>
    <div class="jinfo">→ ${esc(j.note||'')}</div></div>
    <span class="jact ok">${j.seconds||''}s&nbsp;<a href="#" data-note="${esc(j.note||'')}">xem note</a></span>`
  :`<span class="jstatus fail">✕</span>
    <div style="min-width:0"><div class="jname">${esc(j.name)}</div></div>
    <span class="jact fail">lỗi</span>`;
 qDone.prepend(d);pDone.hidden=false;
 qDoneStat.innerHTML=`<b>${qDone.children.length}</b> file`;
 const c={el:d,status:j.status};cards.set(j.id,c);
 if(qDone.children.length>25)qDone.lastChild.remove();
 return c;}

document.addEventListener('click',e=>{const a=e.target.closest('[data-note]');
 if(a){e.preventDefault();openPanel(a.dataset.note);}});

let seenErr=0,dead=false;
async function poll(){
 if(dead)return;
 try{
  const js=await(await fjson('/jobs')).json();
  const ids=new Set(js.map(j=>j.id));
  js.forEach(j=>lastJobs.set(j.id,j));
  js.forEach(apply);
  cards.forEach((c,id)=>{
   if(!ids.has(id)&&document.getElementById('j_'+id)){c.el.remove();cards.delete(id);}});
  let nRun=0,nPend=0,nDone=0;
  js.forEach(j=>{if(j.status==='running')nRun++;else if(j.status==='pending')nPend++;
   else if(j.status==='done')nDone++;});
  const runJob=js.find(j=>j.status==='running');
  qStat.innerHTML=nRun?`đang OCR <b>${nRun}</b>${runJob&&runJob.name?` · ${esc(runJob.name.slice(0,28))}`:''}`
   :nPend?`chờ <b>${nPend}</b>`:'rảnh';
  if(!js.some(j=>j.status==='pending'||j.status==='running')&&
     !qActive.querySelector('.job:not([data-err])'))
   qEmpty.style.display='';
  seenErr=0;
  try{const m=await(await fjson('/meta')).json();
   if(m.build!==BUILD){dead=true;
    toast('Có phiên bản mới — đang tải lại trang…');
    setTimeout(()=>location.reload(),900);}}catch(e){}
 }catch(e){seenErr++;if(seenErr===2)toast('Mất kết nối tới server.');}}
setInterval(poll,1500);poll();
/* đồng hồ elapsed cho card đang OCR — nhảy mỗi giây */
setInterval(()=>{lastJobs.forEach((j,id)=>{
 const t=document.getElementById('e_'+id);
 if(t&&j.started)t.textContent=((Date.now()/1000-j.started)|0)+'s';});},1000);

/* ---------- Jan project sync (multi-project) ---------- */
let projList=[],selProjs=new Set(),janByProject={},janSig='\u00d8';
function migrateProjStore(){
 const old=localStorage.getItem('vd.proj');
 if(old&&!localStorage.getItem('vd.projs'))
  localStorage.setItem('vd.projs',JSON.stringify([old]));
 try{selProjs=new Set(JSON.parse(localStorage.getItem('vd.projs')||'[]'))}
 catch(e){selProjs=new Set()}}
function showPop(v){$('#projdd').classList.toggle('open',v)}
function projBtnText(){
 const n=selProjs.size;
 $('#projbtn-t').textContent=n===0?'Ch\u1ecdn project\u2026':
  n===1?((projList.find(p=>selProjs.has(p.id))||{}).name||'\u2014')
       :`${n} project`; }
function renderProjPop(){
 const alive=projList.filter(p=>p.alive),dead=projList.filter(p=>!p.alive);
 const row=p=>`<div class="prow ${selProjs.has(p.id)?'on':''}" data-p="${p.id}"
   role="option" aria-selected="${selProjs.has(p.id)}">
   <span class="pchk">\u2713</span><span class="pname">${esc(p.name)}</span>
   <span class="pjmeta">${p.files?p.files+' file \u00b7 '+p.chunks+' chunks':'tr\u1ed1ng'}</span></div>`;
 const drow=d=>`<div class="prow dead">
   <span class="pchk"></span>
   <span style="min-width:0"><span class="pname">${esc(d.name||'(không rõ tên)')}</span>
     <div class="pshort">${d.id}</div></span>
   <span class="pjmeta">${d.files} file \u00b7 ${d.chunks>999?
     (d.chunks/1000).toFixed(1)+'k':d.chunks} chunks</span>
   <button class="ppurge" data-purge="${d.id}"
     title="Chuyển file DB vào Trash — hoàn tác được">Xoá file</button></div>`;
 $('#projpop').innerHTML=
  (alive.length?alive.map(row).join('')
   :'<div class="pempty">Ch\u01b0a c\u00f3 project n\u00e0o trong Jan \u2014 t\u1ea1o project r\u1ed3i quay l\u1ea1i.</div>')
  +(dead.length?`<div class="psep"></div><div class="plabel">\u0110\u00e3 xo\u00e1 trong Jan (file th\u1eeba)</div>${dead.map(drow).join('')}`:'');
 document.querySelectorAll('.prow:not(.dead)').forEach(r=>r.onclick=e=>{
  e.stopPropagation();
  const id=r.dataset.p;
  if(selProjs.has(id))selProjs.delete(id);else selProjs.add(id);
  localStorage.setItem('vd.projs',JSON.stringify([...selProjs]));
  r.classList.toggle('on',selProjs.has(id));
  r.setAttribute('aria-selected',selProjs.has(id));
  projBtnText();loadJan();});
 document.querySelectorAll('.ppurge').forEach(b=>b.onclick=async e=>{
  e.stopPropagation();
  if(!b.classList.contains('armed')){          /* bấm 1: cảnh báo */
   b.classList.add('armed');b.dataset.t=setTimeout(()=>{b.classList.remove('armed');
    b.textContent='Xo\u00e1 file';},2600);
   b.textContent='Ch\u1eafc ch\u1eafn?';return;}
  clearTimeout(b.dataset.t);
  try{const r=await(await fjson('/jan-purge',{method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({id:b.dataset.purge})})).json();
   if(r.ok){toast('\u0110\u00e3 chuy\u1ec3n file v\u00e0o Trash \u2014 ho\u00e0n t\u00e1c \u1edf Finder',true);}
   else toast(r.error||'kh\u00f4ng xo\u00e1 \u0111\u01b0\u1ee3c');}
  catch(err){toast('kh\u00f4ng k\u1ebft n\u1ed1i \u0111\u01b0\u1ee3c');}
  loadProjects();});}
async function loadEmbed(){
 const w=$('#janwarn');
 try{const st=await(await fjson('/jan-embed')).json();
  if(st.ok){w.hidden=true;w.textContent='';}
  else{w.hidden=false;w.textContent='⚠ '+st.reason;}
 }catch(e){}}
async function loadProjects(){
 loadEmbed();
 try{projList=await(await fjson('/projects')).json();}catch(e){projList=[]}
 if(!projList.length){$('#projwrap').style.display='none';renderNotes(lastNotes);return;}
 const ids=new Set(projList.filter(p=>p.alive).map(p=>p.id));
 selProjs=new Set([...selProjs].filter(id=>ids.has(id)));
 let neverChose=true;try{neverChose=localStorage.getItem('vd.projs')===null}catch(e){}
 if(!selProjs.size&&neverChose&&ids.size===1){selProjs=new Set(ids);
  try{localStorage.setItem('vd.projs',JSON.stringify([...ids]))}catch(e){}}
 renderProjPop();projBtnText();loadJan();}
$('#projbtn').onclick=e=>{e.stopPropagation();
 const willOpen=!$('#projdd').classList.contains('open');
 showPop(willOpen);if(willOpen)loadProjects();};
document.addEventListener('click',e=>{
 if(!e.target.closest('#projdd'))showPop(false)});
function noteState(name){
 let k=0;selProjs.forEach(id=>{
  if(janByProject[id]&&janByProject[id][name]!==undefined)k++});
 return {k,n:selProjs.size};}
async function loadJan(force){
 if(!projList.length)return;
 janByProject={};
 if(selProjs.size){
  try{const r=await(await fjson('/jan-state?projects='+
    encodeURIComponent([...selProjs].join(',')))).json();
   const pp=r.projects||{};
   Object.keys(pp).forEach(pid=>{
    const m={};Object.keys(pp[pid]).forEach(p=>{
     m[p.split('/').pop()]=pp[pid][p].chunk_count});
    janByProject[pid]=m;});
  }catch(e){}}
 const owned=lastNotes.filter(n=>noteState(n.name).k>0).length;
 $('#janinfo').innerHTML=`<b>${owned}</b>/${lastNotes.length||'\u2026'} \u0111\u00e3 trong Jan`+
  (selProjs.size>1?` \u00b7 ${selProjs.size} project`:'');
 const sig=JSON.stringify([janByProject,[...selProjs].sort(),lastNotes.map(n=>n.name)]);
 if(sig!==janSig||force){janSig=sig;renderNotes(lastNotes);}}
const syncing=new Map();  /* note -> nhãn đang chạy */  /* note đang đồng bộ — sống qua các lần render lại */
async function syncOne(name,btn){
 if(!selProjs.size){toast('Ch\u1ecdn Jan project tr\u01b0\u1edbc \u0111\u00e3.');return;}
 if(syncing.has(name))return;
 syncing.set(name,'Đang đồng bộ…');renderNotes(lastNotes);
 let tot=0,ok=0,names=[],err='';
 try{const r=await(await fjson('/jan-sync',{method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({notes:[name],projects:[...selProjs]})},900000)).json();
  if(r.error)err=r.error;
  Object.values(r.projects||{}).forEach(pp=>{
   const res=(pp.results||[])[0];
   if(res&&res.ok){ok++;names.push(pp.name);tot+=res.skipped?0:res.chunks;}
   else err=(res&&res.error)||err||'l\u1ed7i';});
 }catch(e){err='kh\u00f4ng k\u1ebft n\u1ed1i \u0111\u01b0\u1ee3c server';}
 syncing.delete(name);
 if(ok)toast(`${names.join(', ')} \u2190 ${name}${tot?` (${tot} chunks)`:''}`,true);
 else toast(err);
 loadJan(true);}
/* xoá note: bấm 1 lần -> "Xoá?" (3s), bấm lần 2 mới xoá (vào Thùng rác) */
document.addEventListener('click',async e=>{const b=e.target.closest('[data-del]');
 if(!b)return;e.preventDefault();const name=b.dataset.del;
 if(!b.classList.contains('arm')){
  document.querySelectorAll('.ib.del.arm').forEach(x=>{x.classList.remove('arm');x.textContent='';x.innerHTML=x.dataset.icon||x.innerHTML});
  b.dataset.icon=b.innerHTML;b.classList.add('arm');b.textContent='Xoá?';
  setTimeout(()=>{if(b.isConnected&&b.classList.contains('arm')){b.classList.remove('arm');b.innerHTML=b.dataset.icon;}},3000);
  return;}
 b.classList.remove('arm');b.classList.add('busy');b.textContent='…';
 try{const r=await(await fjson('/note-delete',{method:'POST',
   headers:{'Content-Type':'application/json'},body:JSON.stringify({name})})).json();
  if(r.ok){
   if(($('#ptitle')||{}).textContent===name.slice(0,-3))closePanel();
   toast(`Đã chuyển ${name} vào Thùng rác`+(r.jan_projects&&r.jan_projects.length?` · gỡ khỏi Jan: ${r.jan_projects.join(', ')}`:'')+' — khôi phục được trong Finder',true);
   lastNotes=lastNotes.filter(n=>n.name!==name);notesSig='';renderNotes(lastNotes);loadNotes();loadJan(true);}
  else{toast(r.detail||r.error||'Không xoá được');b.classList.remove('busy');b.innerHTML=b.dataset.icon;}
 }catch(err){toast('Không xoá được — server có chạy không?');b.classList.remove('busy');b.innerHTML=b.dataset.icon;}});
document.addEventListener('click',async e=>{const b=e.target.closest('[data-jan]');
 if(!b)return;e.preventDefault();const name=b.dataset.jan;
 if(!(b.classList.contains('on')||b.classList.contains('part'))){syncOne(name,b);return;}
 /* đã trong Jan: bấm 1 lần -> "Gỡ khỏi Jan?" (3s), bấm lần 2 mới gỡ */
 if(!b.classList.contains('arm')){
  b.dataset.html=b.innerHTML;b.classList.add('arm');b.textContent='Gỡ khỏi Jan?';
  setTimeout(()=>{if(b.isConnected&&b.classList.contains('arm')){b.classList.remove('arm');b.innerHTML=b.dataset.html;}},3000);
  return;}
 b.classList.remove('arm');syncing.set(name,'Đang gỡ…');renderNotes(lastNotes);
 try{const r=await(await fjson('/jan-unsync',{method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({note:name,projects:[...selProjs]})})).json();
  if(r.ok)toast(`Đã gỡ ${name} khỏi Jan`+(r.removed_from&&r.removed_from.length?`: ${r.removed_from.join(', ')}`:'')+' — note vẫn ở trong thư viện',true);
  else toast(r.detail||r.error||'Không gỡ được');
 }catch(err){toast('Không gỡ được — server có chạy không?');}
 finally{syncing.delete(name);loadJan(true);}});
$('#syncall').onclick=async()=>{
 if(!selProjs.size){toast('Ch\u1ecdn Jan project tr\u01b0\u1edbc \u0111\u00e3.');return;}
 const btn=$('#syncall');btn.classList.add('busy');
 btn.innerHTML='<span class="jspin"></span>Đang đồng bộ…';
 const batch=lastNotes.map(n=>n.name);batch.forEach(n=>syncing.set(n,'Đang đồng bộ…'));renderNotes(lastNotes);
 let parts=[],anyFail=false;
 try{const r=await(await fjson('/jan-sync',{method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({notes:[],projects:[...selProjs]})},900000)).json();
  if(r.error){toast(r.error);anyFail=true;}
  Object.values(r.projects||{}).forEach(pp=>{
   const s=pp.summary||{};
   if(s.error){parts.push(`${pp.name}: ${s.error}`);anyFail=true;return;}
   const rm=(s.removed||[]).length;
   parts.push(`${pp.name}: m\u1edbi ${(s.synced||[]).length}, b\u1ecf qua ${(s.skipped||[]).length}`+
    (rm?`, dọn ${rm} note đã xoá/di chuyển`:''));
   if((s.failed||[]).length)anyFail=true;});
  if(parts.length)toast(parts.join(' \u00b7 '),!anyFail);
 }catch(e){toast('\u0110\u1ed3ng b\u1ed9 l\u1ed7i \u2014 server c\u00f3 ch\u1ea1y kh\u00f4ng?');}
 batch.forEach(n=>syncing.delete(n));
 btn.classList.remove('busy');btn.textContent='\u0110\u1ed3ng b\u1ed9 t\u1ea5t c\u1ea3';
 loadJan(true);};
async function autoJan(note){
 if(!selProjs.size||!note||syncing.has(note))return;
 syncing.set(note,'Đang đồng bộ…');renderNotes(lastNotes);
 try{const r=await(await fjson('/jan-sync',{method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({notes:[note],projects:[...selProjs]})},900000)).json();
  if(r.error){toast('Không tự đồng bộ được vào Jan: '+r.error);loadEmbed();}
  else{
  const bad=[];const names=[];
  Object.values(r.projects||{}).forEach(pp=>{const res=(pp.results||[])[0];
   if(res&&res.ok)names.push(pp.name);else bad.push(pp.name+': '+((res&&res.error)||'lỗi'));});
  if(bad.length)toast('Tự đồng bộ lỗi — '+bad.join(' · '));
  else if(names.length)toast(`T\u1ef1 \u0111\u1ed9ng v\u00e0o ${names.join(', ')}: ${note}`,true);}
  }catch(e){toast('Tự đồng bộ vào Jan lỗi — server có chạy không?');}
 finally{syncing.delete(note);loadJan(true);}}

/* ---------- notes / library ---------- */
let notesSig='',filterF='all';
document.querySelectorAll('.chip').forEach(ch=>ch.onclick=()=>{
 document.querySelectorAll('.chip').forEach(c=>c.classList.remove('on'));
 ch.classList.add('on');filterF=ch.dataset.f;renderNotes(lastNotes);});
let lastNotes=[];
function gkey(n){return n.name+'|'+n.mtime+'|'+n.kb}
async function loadNotes(){
 let ns;try{ns=await(await fjson('/notes')).json();}catch(e){return;}
 const sig=JSON.stringify(ns.map(gkey));
 lastNotes=ns;
 $('#navCount').textContent=ns.length;
 if(sig===notesSig)return;
 notesSig=sig;
 $('#libsub').textContent=ns.length?`${ns.length} note đã OCR — bấm tên để xem trước`
  :'Vault chưa có note nào — nạp file ở tab "Nạp tài liệu"';
 renderNotes(ns);}
let noteLimit=25;
function renderNotes(ns){
 const cnt={all:ns.length,Inbox:0,Notes:0};ns.forEach(n=>{if(n.folder in cnt)cnt[n.folder]++});
 document.querySelectorAll('.cnt').forEach(c=>c.textContent=cnt[c.dataset.c]||'');
 const nq=(($('#nq')||{}).value||'').trim().toLowerCase();
 ns=ns.filter(n=>(filterF==='all'||n.folder===filterF)&&(!nq||n.name.toLowerCase().includes(nq)));
 const total=ns.length;
 ns=ns.slice(0,noteLimit);
 const ST=ns.map(n=>{const s=noteState(n.name);
  if(syncing.has(n.name))return {...s,cls:'busy',busy:true,txt:syncing.get(n.name)};
  const cls=s.n===0||s.k===0?'':(s.k===s.n?'on':'part');
  return {...s,cls,
   txt:s.n===0?'Chọn project':(s.k===s.n?'Trong Jan':(s.k>0?`Jan ${s.k}/${s.n}`:'Đưa vào Jan'))};});
 if(!ns.length&&!total){$('#notes').innerHTML=`<div class="empty">
  <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#8f99a5"
   stroke-width="1.5"><path d="M4 5a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/></svg>
  <div>${
  nq?'Không có note nào khớp “'+esc(nq)+'”':filterF==='all'?'Vault còn trống':'Chưa có note trong '+(filterF==='Inbox'?'Inbox':'Notes')}</div></div>`;return;}
 $('#notes').innerHTML=ns.map((n,i)=>{const st=ST[i];return `<div class="nrow" style="animation-delay:${Math.min(i,12)*30}ms">
  <span class="ftag">${n.folder}</span>
  <span class="nname"><a data-note="${esc(n.name)}" title="${esc(n.name)} — bấm để xem trước">${esc(n.name)}</a></span>
  <span class="ninfo">${n.mtime} · ${n.kb} KB</span>
  <span class="acts">
   <a class="jb ${st.cls}" data-jan="${esc(n.name)}"
     title="${selProjs.size?((st.cls==='on'||st.cls==='part')?'Đã có trong Jan — bấm để gỡ khỏi Jan (note vẫn giữ trong thư viện)':'Đồng bộ vào '+selProjs.size+' project đang chọn'):'Chọn Jan project ở trên'}">${st.busy?'<span class="jspin"></span>':'<span class="jd"></span>'}${st.txt}</a>
   <a class="ib" data-note="${esc(n.name)}" title="Xem trước" aria-label="Xem trước ${esc(n.name)}">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-6.5 10-6.5S22 12 22 12s-3.5 6.5-10 6.5S2 12 2 12z"/><circle cx="12" cy="12" r="2.6"/></svg></a>
   <a class="ib" href="/download/${encodeURIComponent(n.name)}" title="Tải file .md" aria-label="Tải ${esc(n.name)}">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11m0 0-4-4m4 4 4-4"/><path d="M4 20h16"/></svg></a>
   <button class="ib del" data-del="${esc(n.name)}" title="Xoá khỏi thư viện" aria-label="Xoá ${esc(n.name)}">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/></svg></button></span></div>`}).join('')
  +(total>noteLimit?`<div class="lmore"><button class="btn2" id="loadmore">Tải thêm · còn ${total-noteLimit} note</button></div>`:'');}
$('#nq').addEventListener('input',()=>renderNotes(lastNotes));
document.addEventListener('click',e=>{if(e.target.closest('#loadmore')){
 noteLimit+=50;renderNotes(lastNotes);}});

/* ---------- preview panel ---------- */
const panel=$('#panel'),scrim=$('#scrim');
const escH=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
function mdInline(s){return escH(s)
  .replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>')
  .replace(/\*([^*]+)\*/g,'<i>$1</i>')
  .replace(/`([^`]+)`/g,'<code>$1</code>');}
function mdToHtml(t){
 let fm='';
 const m=t.match(/^---\\n([\s\S]*?)\\n---\\n/);
 if(m){t=t.slice(m[0].length);
  fm='<div class="pmeta">'+m[1].split('\\n').map(l=>{const i=l.indexOf(':');
   return i<0?escH(l):'<b>'+escH(l.slice(0,i+1))+'</b>'+escH(l.slice(i+1));}).join('<br>')+'</div>';}
 let html='',list=null,para=[];
 const flush=()=>{if(para.length){html+='<p>'+para.map(mdInline).join('<br>')+'</p>';para=[];}};
 const endList=()=>{if(list){html+='</'+list+'>';list=null;}};
 for(const raw of t.split('\\n')){
  const l=raw.trim();
  if(!l){flush();endList();continue;}
  if(l==='---'){flush();endList();html+='<hr>';continue;}
  const h=l.match(/^(#{1,5})\s+(.*)$/);
  if(h){flush();endList();const n=Math.min(h[1].length+1,5);
   html+='<h'+n+'>'+mdInline(h[2])+'</h'+n+'>';continue;}
  if(/^[-*]\s+/.test(l)){flush();
   if(list!=='ul'){endList();list='ul';html+='<ul>';}
   html+='<li>'+mdInline(l.replace(/^[-*]\s+/,''))+'</li>';continue;}
  if(/^\d+[.)]\s+/.test(l)){flush();
   if(list!=='ol'){endList();list='ol';html+='<ol>';}
   html+='<li>'+mdInline(l.replace(/^\d+[.)]\s+/,''))+'</li>';continue;}
  if(list)endList();
  para.push(raw);}
 flush();endList();
 return fm+html;}
let lastNoteText='',pmode='view';
function paintPanel(){
 if(pmode==='view'){$('#pview').innerHTML=mdToHtml(lastNoteText);}
 else{$('#praw').textContent=lastNoteText;}
 $('#pview').hidden=pmode!=='view';
 $('#praw').hidden=pmode==='view';
 document.querySelectorAll('[data-pm]').forEach(x=>
  x.classList.toggle('on',x.dataset.pm===pmode));}
async function openPanel(name){
 $('#ptitle').textContent=name.replace(/\.md$/i,'');
 $('#pview').innerHTML='';$('#praw').textContent='Đang tải…';
 $('#pdl').href='/download/'+encodeURIComponent(name);
 panel.classList.add('open');scrim.classList.add('on');
 try{lastNoteText=await(await fjson('/note/'+encodeURIComponent(name))).text();}
 catch(e){lastNoteText='Không đọc được note này.';}
 paintPanel();}
function closePanel(){panel.classList.remove('open');scrim.classList.remove('on');}
$('#px').onclick=closePanel;scrim.onclick=closePanel;
document.addEventListener('keydown',e=>{if(e.key==='Escape')closePanel();});
document.addEventListener('click',e=>{const b=e.target.closest('[data-pm]');
 if(b){pmode=b.dataset.pm;paintPanel();}});

function toast(msg,ok){const t=$('#toast');t.textContent=msg;
 t.classList.toggle('ok',!!ok);t.classList.add('on');
 setTimeout(()=>t.classList.remove('on'),3200);}
try{restoreLangs();paintThemeBtn();migrateProjStore();loadProjects();loadNotes();
 setInterval(loadNotes,8000);
 setInterval(()=>{if(!document.hidden&&!$('#projdd').classList.contains('open'))loadProjects()},10000);
 const _b=$('#jsbadge');
 if(_b){_b.textContent='JS OK';}  /* chỉ hiện khi lỗi (xem __vderr) */
}catch(e){
 const _b=$('#jsbadge');
 if(_b){_b.style.opacity='1';_b.textContent='JS lỗi: '+e.message;_b.style.color='#e05d4d';}
 console.error(e);}
</script></body></html>"""


FONT_DIR = os.path.join(HERE, "static", "fonts")


@app.get("/fonts/{name}")
def fonts(name: str):
    """Font đóng gói sẵn trong repo — web UI chạy offline hoàn toàn."""
    fp = os.path.join(FONT_DIR, os.path.basename(name))
    if not (name.endswith((".woff2", ".css")) and os.path.isfile(fp)):
        raise HTTPException(404)
    return FileResponse(fp, headers={"Cache-Control": "max-age=86400"})


@app.middleware("http")
async def no_store_cache(request: Request, call_next):
    resp = await call_next(request)
    if not request.url.path.startswith("/fonts/"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE.replace("__BUILD__", BUILD)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
