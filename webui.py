#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""webui.py — Local web upload UI for jan-vision-docs-mcp (localhost only).

Binds 127.0.0.1 only — no LAN exposure, no auth. Serves:
  GET  /            — drag & drop upload page with live OCR status
  POST /upload      — multipart upload (pdf/docx/md/txt/images), queued
  GET  /jobs        — JSON status of every job (pending/running/done/failed)
  GET  /notes       — JSON list of vault notes (Inbox + Notes)
  GET  /note/<name> — preview a note as text (HTML-escaped)
  GET  /download/<name> — download a .md note
  GET  /healthz     — "ok"

Files are staged in <repo>/uploads/ (NOT _dropbox — the launchd watcher
must not double-process them) and ingested via server_extra.ingest_document.

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

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
import uvicorn

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PDF_OCR_WEBUI_PORT", "8765"))
VAULT = os.path.abspath(os.path.expanduser(
    os.environ.get("PDF_OCR_VAULT", "~/Documents/Vault")))
STAGE = os.path.join(HERE, "uploads")
os.makedirs(STAGE, exist_ok=True)

SUPPORTED = {".pdf", ".docx", ".md", ".markdown", ".txt", ".text",
             ".png", ".jpg", ".jpeg", ".heic", ".heif", ".tiff", ".tif",
             ".webp", ".bmp", ".gif"}

app = FastAPI(title="jan-vision-docs web UI", docs_url=None, redoc_url=None)

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
        t0 = time.time()
        try:
            r = json.loads(se.ingest_document(path=job["src"]))
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
        finally:
            job["done_at"] = time.time()


threading.Thread(target=_worker, daemon=True).start()


@app.get("/healthz")
def healthz():
    return "ok"


@app.post("/upload")
async def upload(files: list[UploadFile] = File(...)):
    out = []
    for f in files:
        base = os.path.basename(f.filename or "file")
        ext = os.path.splitext(base)[1].lower()
        if ext not in SUPPORTED:
            out.append({"name": base, "error": f"Định dạng không hỗ trợ: {ext}"})
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
                     "status": "pending", "added": time.time()}
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
                                  "%H:%M:%S %d/%m", time.localtime(
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


PAGE = """<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vision Docs — Vault</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;font:15px/1.5 -apple-system,system-ui,sans-serif;
     background:#0d0f12;color:#e6e8ea}
header{padding:18px 28px;border-bottom:1px solid #1e222a;display:flex;
       justify-content:space-between;align-items:center}
h1{font-size:17px;margin:0}small{color:#8b929d}
main{max-width:760px;margin:0 auto;padding:24px 20px}
#drop{border:2px dashed #3a4150;border-radius:14px;padding:44px 20px;
      text-align:center;color:#aab2bd;transition:.15s;cursor:pointer}
#drop.over{border-color:#f0805a;color:#f0805a;background:#1a150f10}
#drop b{color:#e6e8ea}
.card{background:#151920;border:1px solid #232934;border-radius:10px;
      padding:12px 16px;margin-top:12px}
.card .row{display:flex;justify-content:space-between;gap:10px}
.spin{display:inline-block;width:13px;height:13px;border:2px solid #556;
      border-top-color:#f0805a;border-radius:50%;animation:r 0.8s linear infinite}
@keyframes r{to{transform:rotate(360deg)}}
.done{color:#5fbf78}.fail{color:#e0705f}.pend{color:#aab2bd}
table{width:100%;border-collapse:collapse;font-size:14px}
td{padding:7px 8px;border-bottom:1px solid #1e222a}
td a{color:#f0805a;text-decoration:none}td a:hover{text-decoration:underline}
h2{font-size:14px;color:#8b929d;text-transform:uppercase;letter-spacing:.4px;
   margin:26px 0 4px}
</style></head><body>
<header><h1>📁 Vision Docs Vault</h1>
<small id="vault">""" + html.escape(VAULT) + """</small></header>
<main>
<div id="drop"><b>Thả file vào đây</b> hoặc bấm để chọn<br>
<small>PDF · DOCX · MD · TXT · ảnh — tự OCR bằng Vision rồi ghi note vào Inbox</small>
<input id="file" type="file" multiple hidden
       accept=".pdf,.docx,.md,.markdown,.txt,.text,.png,.jpg,.jpeg,.heic,.tiff,.webp,.bmp">
</div>
<div id="jobs"></div>
<h2>Notes trong vault</h2>
<table id="notes"></table>
</main><script>
const $=s=>document.querySelector(s);
const drop=$('#drop'),fi=$('#file'),jobsEl=$('#jobs'),notesEl=$('#notes');
drop.onclick=()=>fi.click();
['dragover','dragleave','drop'].forEach(ev=>drop.addEventListener(ev,e=>{
 e.preventDefault();drop.classList.toggle('over',ev==='dragover');
 if(ev==='drop')send(e.dataTransfer.files)}));
fi.onchange=()=>send(fi.files);
async function send(fs){
 const fd=new FormData();
 [...fs].forEach(f=>fd.append('files',f));
 if(!fs.length)return;
 drop.classList.remove('over');
 const r=await(await fetch('/upload',{method:'POST',body:fd})).json();
 r.jobs.forEach(j=>{if(j.error){
   const d=document.createElement('div');d.className='card';
   d.innerHTML=`<div class="row"><span>${j.name}</span>
   <span class="fail">${j.error}</span></div>`;jobsEl.prepend(d);}
  else addCard(j.id,j.name);});fi.value='';}
function addCard(id,name){
 const d=document.createElement('div');d.className='card';
 d.innerHTML=`<div class="row"><span>${name}</span>
 <span class="pend" id="s_${id}">đang chờ…</span></div>`;
 jobsEl.prepend(d);}
function render(j){
 let el=document.getElementById('s_'+j.id);
 if(!el){addCard(j.id,j.name);el=document.getElementById('s_'+j.id);}
 if(j.status==='pending')el.className='pend',el.textContent='đang chờ…';
 if(j.status==='running')el.className='pend',
   el.innerHTML='<span class="spin"></span> đang OCR…';
 if(j.status==='done')el.className='done',
   el.textContent=`xong (${j.seconds}s) → ${j.note}`;
 if(j.status==='failed')el.className='fail',el.textContent='lỗi: '+j.error;}
async function poll(){
 try{const js=await(await fetch('/jobs')).json();
  const ids=new Set(js.map(j=>j.id));js.forEach(render);
  document.querySelectorAll('#jobs .card').forEach(c=>{
   if(![...c.querySelectorAll('[id]')].some(e=>ids.has(e.id.slice(2))))
     c.remove();});}catch(e){}}
async function loadNotes(){
 const ns=await(await fetch('/notes')).json();
 notesEl.innerHTML=ns.map(n=>`<tr><td>${n.folder}/</td>
  <td><a href="/note/${encodeURIComponent(n.name)}">${n.name}</a></td>
  <td style="color:#8b929d">${n.mtime} · ${n.kb} KB</td>
  <td style="text-align:right"><a href="/download/${encodeURIComponent(n.name)}">tải .md</a></td>
 </tr>`).join('')||'<tr><td style="color:#8b929d">(vault trống)</td></tr>';}
setInterval(poll,1500);poll();setInterval(loadNotes,8000);loadNotes();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
