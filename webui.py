#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""webui.py — Local web upload UI for jan-vision-docs-mcp (localhost only).

Binds 127.0.0.1 only — no LAN exposure, no auth. Serves:
  GET  /            — dashboard UI (upload queue + library), localhost only
  POST /upload       — multipart upload (pdf/docx/md/txt/images), queued
  GET  /jobs        — JSON status of every job (pending/running/done/failed)
  GET  /notes       — JSON list of vault notes (Inbox + Notes)
  GET  /note/<name>  — preview a note as text (HTML-escaped)
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
import json
import os
import queue
import threading
import time
import uuid

import jan_sync
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
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
    txt = html.escape(open(fp, encoding="utf-8", errors="replace").read())
    return PlainTextResponse(txt, media_type="text/plain; charset=utf-8")


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
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
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
aside{position:sticky;top:0;height:100dvh;display:flex;flex-direction:column;
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
.vault{font:400 10.5px/1.5 var(--mono);color:var(--faint);word-break:break-all}
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
.chips{display:flex;gap:7px}
.chip{font:500 12px var(--sans);color:var(--muted);cursor:pointer;
  background:none;border:1px solid var(--line);padding:6px 13px;border-radius:999px;
  transition:color .18s,border-color .18s,background .18s}
.chip:hover{color:var(--text)}
.chip.on{color:var(--text);border-color:#e08a5c44;background:#e08a5c10}
#notes{margin-top:16px;display:grid;gap:8px}
.nrow{display:grid;grid-template-columns:auto 1fr 2fr auto;gap:14px;align-items:center;
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
.acts{display:flex;gap:8px;justify-content:flex-end}
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
  display:flex;flex-direction:column;box-shadow:-30px 0 70px -40px #000000d9}
#panel.open{transform:none}
.pheadp{display:flex;align-items:center;gap:12px;padding:15px 20px;
  border-bottom:1px solid var(--line)}
.pheadp .t{font-weight:600;font-size:14.5px;flex:1;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.pbody{flex:1;overflow:auto;padding:18px 20px;margin:0}
.pbody pre{white-space:pre-wrap;word-break:break-word;margin:0;
  font:400 12.5px/1.7 var(--mono);color:#cdd4dc}
#scrim{position:fixed;inset:0;background:#00000066;opacity:0;pointer-events:none;
  transition:opacity .25s;z-index:15}
#scrim.on{opacity:1;pointer-events:auto}
.xbtn{background:var(--panel2);border:1px solid var(--line);color:var(--muted);
  width:30px;height:30px;border-radius:8px;cursor:pointer;font-size:15px;flex:none;
  transition:color .2s,border-color .2s}
.xbtn:hover{color:var(--text)}
.xbtn:active{transform:translateY(1px)}
.toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,80px);
  background:var(--panel2);border:1px solid #d9695a44;color:var(--err);
  padding:11px 18px;border-radius:11px;font-size:13.5px;z-index:30;
  transition:transform .3s cubic-bezier(.2,.8,.25,1);box-shadow:var(--shadow)}
.toast.on{transform:translate(-50%,0)}

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
.prow:hover{background:#ffffff07;color:var(--text)}
.pchk{width:17px;height:17px;border-radius:5px;border:1px solid var(--line);
 display:grid;place-items:center;font-size:11px;flex:none;color:transparent;
 transition:background .14s,border-color .14s,color .14s}
.prow.on .pchk{background:var(--accent-dim);border-color:var(--accent);color:#fff}
.prow.on .pname{color:var(--text)}
.pname{font-weight:500;font-size:13.5px}
.pmeta{margin-left:auto;font:400 10.5px var(--mono);color:var(--faint)}
.pempty{padding:11px 12px;color:var(--faint);font-size:13px}

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
.jb{font:500 11.5px var(--mono);color:var(--faint);border:1px solid var(--line);.jb{display:inline-flex;align-items:center;gap:5px;
 padding:5px 9px;border-radius:7px;cursor:pointer;text-decoration:none;
 transition:color .18s,border-color .18s;white-space:nowrap}
.jb:hover{color:var(--text);border-color:var(--line-strong)}
.jb.on{color:var(--ok);border-color:#4fb07133}
.jb.busy{opacity:.4;pointer-events:none}
.jb.okpop{animation:pop .4s ease}
.toast.ok{border-color:#4fb07144;color:var(--ok)}
.ic-moon{display:none}[data-theme="light"] .ic-sun{display:none}
[data-theme="light"] .ic-moon{display:block}
[data-theme="light"] .toast{box-shadow:0 12px 30px -14px #00000040}
[data-theme="light"] .projpop{box-shadow:0 24px 60px -18px #00000030,0 1px 0 #ffffff60 inset}
[data-theme="light"] ::selection{background:#c0673533}

@media (max-width:860px){
  #shell{grid-template-columns:1fr}
  aside{position:static;height:auto;flex-direction:row;align-items:center;
    padding:12px 16px;border-right:0;border-bottom:1px solid var(--line);gap:4px}
  .brand{padding:0 14px 0 0}
  .navlbl,.sidefoot .vault{display:none}
  .nav{width:auto;padding:9px 12px}
  .sidefoot{margin:0 0 0 auto;border:0;padding:0;flex-direction:row}
  main{padding:24px 18px 70px}
  .nrow{grid-template-columns:1fr auto;row-gap:8px}
  .ninfo{grid-column:1;text-align:left}
}
</style></head><body>
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
      <span class="srv"><span class="dot"></span> server đang chạy</span>
      <button class="xbtn" id="themebtn" aria-label="Chuyển sáng/tối" title="Sáng / tối"
        style="width:26px;height:26px;font-size:12px">
        <svg class="ic-sun" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
        <svg class="ic-moon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>
      </button>
    </div>
    <div class="vault" title=\"""" + html.escape(VAULT) + """">""" + html.escape(VAULT) + """</div>
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
    <div class="vhead vrow">
      <div><h1>Thư viện</h1><p id="libsub">—</p></div>
      <div class="chips">
        <button class="chip on" data-f="all">Tất cả</button>
        <button class="chip" data-f="Inbox">Inbox</button>
        <button class="chip" data-f="Notes">Notes</button>
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
    </div>
    <div id="notes"></div>
  </section>
</main>
</div>

<div id="scrim"></div>
<aside id="panel" aria-label="Xem trước note">
  <div class="pheadp"><span class="t" id="ptitle">—</span>
    <a id="pdl" href="#" style="font:500 13px var(--sans);color:var(--accent);
      text-decoration:none;padding:7px 12px;border:1px solid #e08a5c33;border-radius:8px">Tải .md</a>
    <button class="xbtn" id="px" aria-label="Đóng">✕</button></div>
  <div class="pbody"><pre id="pbody"></pre></div>
</aside>
<div class="toast" id="toast"></div>

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
$('#themebtn').onclick=()=>{
 const cur=document.documentElement.dataset.theme==='dark'?'dark':'light';
 const next=cur==='dark'?'light':'dark';
 document.documentElement.dataset.theme=next;
 localStorage.setItem('vd.theme',next);};

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
 $('#projpop').innerHTML=projList.length?projList.map(p=>
  `<div class="prow ${selProjs.has(p.id)?'on':''}" data-p="${p.id}"
   role="option" aria-selected="${selProjs.has(p.id)}">
   <span class="pchk">\u2713</span><span class="pname">${esc(p.name)}</span>
   <span class="pmeta">${p.files} file \u00b7 ${p.chunks} chunks</span></div>`).join('')
  :'<div class="pempty">Ch\u01b0a c\u00f3 project n\u00e0o trong Jan \u2014 t\u1ea1o project r\u1ed3i quay l\u1ea1i.</div>';
 document.querySelectorAll('.prow').forEach(r=>r.onclick=e=>{
  e.stopPropagation();
  const id=r.dataset.p;
  if(selProjs.has(id))selProjs.delete(id);else selProjs.add(id);
  localStorage.setItem('vd.projs',JSON.stringify([...selProjs]));
  r.classList.toggle('on',selProjs.has(id));
  r.setAttribute('aria-selected',selProjs.has(id));
  projBtnText();loadJan();});}
async function loadProjects(){
 try{projList=await(await fjson('/projects')).json();}catch(e){projList=[]}
 if(!projList.length){$('#projwrap').style.display='none';renderNotes(lastNotes);return;}
 const ids=new Set(projList.map(p=>p.id));
 selProjs=new Set([...selProjs].filter(id=>ids.has(id)));
 renderProjPop();projBtnText();loadJan();}
$('#projbtn').onclick=e=>{e.stopPropagation();
 showPop(!$('#projdd').classList.contains('open'))};
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
async function syncOne(name,btn){
 if(!selProjs.size){toast('Ch\u1ecdn Jan project tr\u01b0\u1edbc \u0111\u00e3.');return;}
 if(btn)btn.classList.add('busy');
 let tot=0,ok=0,names=[],err='';
 try{const r=await(await fjson('/jan-sync',{method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({notes:[name],projects:[...selProjs]})})).json();
  if(r.error)err=r.error;
  Object.values(r.projects||{}).forEach(pp=>{
   const res=(pp.results||[])[0];
   if(res&&res.ok){ok++;names.push(pp.name);tot+=res.skipped?0:res.chunks;}
   else err=(res&&res.error)||err||'l\u1ed7i';});
 }catch(e){err='kh\u00f4ng k\u1ebft n\u1ed1i \u0111\u01b0\u1ee3c server';}
 if(btn)btn.classList.remove('busy');
 if(ok)toast(`${names.join(', ')} \u2190 ${name}${tot?` (${tot} chunks)`:''}`,true);
 else toast(err);
 loadJan(true);}
document.addEventListener('click',e=>{const b=e.target.closest('[data-jan]');
 if(b){e.preventDefault();syncOne(b.dataset.jan,b);}});
$('#syncall').onclick=async()=>{
 if(!selProjs.size){toast('Ch\u1ecdn Jan project tr\u01b0\u1edbc \u0111\u00e3.');return;}
 const btn=$('#syncall');btn.classList.add('busy');btn.textContent='\u0110ang \u0111\u1ed3ng b\u1ed9\u2026';
 let parts=[],anyFail=false;
 try{const r=await(await fjson('/jan-sync',{method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({notes:[],projects:[...selProjs]})})).json();
  if(r.error){toast(r.error);return;}
  Object.values(r.projects||{}).forEach(pp=>{
   const s=pp.summary||{};
   parts.push(`${pp.name}: m\u1edbi ${(s.synced||[]).length}, b\u1ecf qua ${(s.skipped||[]).length}`);
   if((s.failed||[]).length)anyFail=true;});
  toast(parts.join(' \u00b7 '),!anyFail);
 }catch(e){toast('\u0110\u1ed3ng b\u1ed9 l\u1ed7i \u2014 server c\u00f3 ch\u1ea1y kh\u00f4ng?');}
 btn.classList.remove('busy');btn.textContent='\u0110\u1ed3ng b\u1ed9 t\u1ea5t c\u1ea3';
 loadJan(true);};
async function autoJan(note){
 if(!selProjs.size||!note)return;
 try{const r=await(await fjson('/jan-sync',{method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({notes:[note],projects:[...selProjs]})})).json();
  const names=Object.values(r.projects||{}).map(pp=>pp.name);
  if(names.length)toast(`T\u1ef1 \u0111\u1ed9ng v\u00e0o ${names.join(', ')}: ${note}`,true);
  loadJan(true);}catch(e){}}

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
function renderNotes(ns){
 ns=ns.filter(n=>filterF==='all'||n.folder===filterF);
 const ST=ns.map(n=>{const s=noteState(n.name);
  return {...s,cls:s.n===0?'':'on',
   txt:s.n===0?(selProjs.size?'\u2192 Jan':'Jan')
    :(s.k===s.n?'\u2713 Jan':(s.k>0?`\u25d0 ${s.k}/${s.n}`:'\u2192 Jan'))};});
 if(!ns.length){$('#notes').innerHTML=`<div class="empty">
  <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#8f99a5"
   stroke-width="1.5"><path d="M4 5a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/></svg>
  <div>${filterF==='all'?'Vault còn trống':'Chưa có note trong '+(filterF==='Inbox'?'Inbox':'Notes')}</div></div>`;return;}
 $('#notes').innerHTML=ns.map((n,i)=>{const st=ST[i];return `<div class="nrow" style="animation-delay:${Math.min(i,12)*30}ms">
  <span class="ftag">${n.folder}</span>
  <span class="nname"><a data-note="${esc(n.name)}" title="Xem trước">${esc(n.name)}</a></span>
  <span class="ninfo">${n.mtime} · ${n.kb} KB</span>
  <span class="acts">
   <a class="jb ${st.cls}" data-jan="${esc(n.name)}"
     title="${selProjs.size?('Đồng bộ vào '+selProjs.size+' project đang chọn'):'Chọn Jan project ở trên'}">
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8h18l-1.4 11.2a2 2 0 0 1-2 1.8H6.4a2 2 0 0 1-2-1.8L3 8z"/><path d="M8 8V6a4 4 0 0 1 8 0v2"/></svg>${st.txt}</a>
   <a data-note="${esc(n.name)}" title="Xem trước note">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-6.5 10-6.5S22 12 22 12s-3.5 6.5-10 6.5S2 12 2 12z"/><circle cx="12" cy="12" r="2.6"/></svg>Xem</a>
   <a href="/download/${encodeURIComponent(n.name)}" title="Tải file .md">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11m0 0-4-4m4 4 4-4"/><path d="M4 20h16"/></svg>Tải .md</a></span></div>`}).join('');}

/* ---------- preview panel ---------- */
const panel=$('#panel'),scrim=$('#scrim');
async function openPanel(name){
 $('#ptitle').textContent=name.replace(/\\.md$/i,'');
 $('#pbody').textContent='Đang tải…';
 $('#pdl').href='/download/'+encodeURIComponent(name);
 panel.classList.add('open');scrim.classList.add('on');
 try{const t=await(await fjson('/note/'+encodeURIComponent(name))).text();
  $('#pbody').textContent=t;}
 catch(e){$('#pbody').textContent='Không đọc được note này.';}}
function closePanel(){panel.classList.remove('open');scrim.classList.remove('on');}
$('#px').onclick=closePanel;scrim.onclick=closePanel;
document.addEventListener('keydown',e=>{if(e.key==='Escape')closePanel();});

function toast(msg,ok){const t=$('#toast');t.textContent=msg;
 t.classList.toggle('ok',!!ok);t.classList.add('on');
 setTimeout(()=>t.classList.remove('on'),3200);}
restoreLangs();migrateProjStore();loadProjects();loadNotes();setInterval(loadNotes,8000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE.replace("__BUILD__", BUILD)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
