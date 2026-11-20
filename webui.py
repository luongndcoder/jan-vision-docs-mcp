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
import os as _os
BUILD = str(int(_os.path.getmtime(__file__)))

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


threading.Thread(target=_worker, daemon=True).start()


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/meta")
def meta():
    return {"build": BUILD}


@app.post("/upload")
async def upload(files: list[UploadFile] = File(...)):
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
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);
  font:400 15px/1.55 var(--sans);
  background-image:radial-gradient(900px 480px at 85% -10%,#e08a5c12,transparent 60%),
                   radial-gradient(700px 420px at -10% 110%,#27413a14,transparent 65%)}
body::before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.5;z-index:0;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2'/%3E%3CfeColorMatrix values='0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 0 0 0 0.04 0'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)'/%3E%3C/svg%3E")}
::selection{background:#e08a5c33}
.mono{font-family:var(--mono)}
a{color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}

header{position:sticky;top:0;z-index:5;backdrop-filter:blur(14px);
  background:#0c0e11b8;border-bottom:1px solid var(--line)}
.hwrap{max-width:920px;margin:0 auto;padding:14px 24px;display:flex;
  align-items:center;gap:14px}
.brand{display:flex;align-items:center;gap:10px;font-weight:600;font-size:16px;
  letter-spacing:-.01em}
.mark{width:30px;height:30px;border-radius:9px;background:linear-gradient(145deg,#e89a6d,#c8704a);
  display:grid;place-items:center;flex:none;
  box-shadow:0 4px 14px -4px #e08a5c55,0 0 0 1px #ffffff14 inset}
.mark svg{width:16px;height:16px}
.vault{font:400 12px/1 var(--mono);color:var(--muted);padding:7px 11px;
  border:1px solid var(--line);border-radius:8px;background:var(--panel);
  margin-left:auto;max-width:340px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ok);flex:none;
  box-shadow:0 0 8px #4fb07188;animation:breathe 2.4s ease-in-out infinite}
@keyframes breathe{50%{opacity:.55}}

main{max-width:920px;margin:0 auto;padding:34px 24px 90px;position:relative;z-index:1}
.hero{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin-bottom:22px;flex-wrap:wrap}
h1{font-size:clamp(26px,4vw,34px);font-weight:600;letter-spacing:-.025em;
  margin:0;line-height:1.15;text-wrap:balance}
.hero p{margin:6px 0 0;color:var(--muted);font-size:14.5px;max-width:52ch}
.count{font:500 12px/1 var(--mono);color:var(--muted);letter-spacing:.02em;
  padding:8px 12px;border:1px solid var(--line);border-radius:8px;
  background:var(--panel);white-space:nowrap}
.count b{color:var(--text);font-weight:600}

#drop{position:relative;cursor:pointer;overflow:hidden;
  border:1px solid var(--line);border-radius:18px;background:var(--panel);
  padding:46px 22px;text-align:center;transition:border-color .25s,transform .15s;
  --mx:50%;--my:50%}
#drop::before{content:"";position:absolute;inset:0;opacity:0;transition:opacity .3s;
  background:radial-gradient(320px 200px at var(--mx) var(--my),#e08a5c14,transparent 70%)}
#drop:hover,#drop.over{border-color:var(--accent-dim)}
#drop.over{transform:scale(.995);border-color:var(--accent)}
#drop:hover::before,#drop.over::before{opacity:1}
#drop .tz{width:54px;height:54px;margin:0 auto 16px;border-radius:15px;
  background:var(--panel2);border:1px solid var(--line);display:grid;place-items:center;
  transition:transform .25s cubic-bezier(.2,.9,.3,1.2)}
#drop:hover .tz,#drop.over .tz{transform:translateY(-3px)}
#drop b{font-weight:600;font-size:17px;letter-spacing:-.01em}
#drop small{display:block;margin-top:7px;color:var(--muted);font-size:13px}
.kinds{display:flex;gap:6px;justify-content:center;margin-top:18px;flex-wrap:wrap}
.kind{font:500 11px/1 var(--mono);color:var(--faint);padding:5px 9px;
  border:1px solid var(--line);border-radius:6px;background:#0c0e11}
#drop.over .kind{color:var(--accent);border-color:#e08a5c44}

#jobs{margin-top:26px;display:grid;gap:10px}
.job{display:grid;grid-template-columns:20px 1fr auto;gap:14px;align-items:center;
  padding:13px 16px;border-radius:12px;background:var(--panel);
  border:1px solid var(--line);box-shadow:var(--shadow);
  animation:enter .35s cubic-bezier(.2,.8,.3,1) both}
@keyframes enter{from{opacity:0;transform:translateY(7px)}}
@keyframes enter2{from{opacity:0;transform:translateY(9px)}}
.jname{font-weight:500;font-size:14.5px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.jmeta{font:400 12px/1 var(--mono);color:var(--faint);margin-top:3px}
.jstat{font-size:13px;display:inline-flex;align-items:center;gap:8px;white-space:nowrap}
.pend{color:var(--muted)}
.ok{color:var(--ok)}.fail{color:var(--err)}
.spin{width:12px;height:12px;border-radius:50%;flex:none;
  border:1.6px solid #ffffff18;border-top-color:var(--accent);
  animation:r .8s linear infinite}
@keyframes r{to{transform:rotate(360deg)}}
.shimmer{height:2px;border-radius:2px;margin-top:9px;overflow:hidden;background:#ffffff0a}
.shimmer::after{content:"";display:block;height:100%;width:38%;
  background:linear-gradient(90deg,transparent,var(--accent-dim),transparent);
  animation:slide 1.3s ease-in-out infinite}
@keyframes slide{to{transform:translateX(280%)}}
.jstat a{color:var(--accent);text-decoration:none;font:500 13px/1 var(--mono);
  padding:5px 10px;border:1px solid #e08a5c33;border-radius:7px;
  transition:background .2s,transform .1s}
.jstat a:hover{background:#e08a5c14}
.jstat a:active{transform:translateY(1px)}

h2{font-size:13px;font-weight:600;color:var(--faint);letter-spacing:.08em;
  margin:34px 0 6px;text-transform:uppercase}
#notes{display:grid;gap:8px}
.nrow{display:grid;grid-template-columns:auto 1fr 2fr auto;gap:14px;align-items:center;
  padding:12px 16px;border-radius:12px;background:var(--panel);
  border:1px solid transparent;transition:border-color .2s,transform .15s;
  animation:enter2 .4s ease both}
@keyframes cb{to{opacity:1;transform:none}}
.nrow:hover{border-color:var(--line)}
.ftag{font:500 11px/1 var(--mono);color:var(--faint);padding:4px 8px;
  border:1px solid var(--line);border-radius:6px}
.nname{font-weight:500;font-size:14.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nname a{text-decoration:none;cursor:pointer}
.nname a:hover{color:var(--accent)}
.ninfo{font:400 12px/1 var(--mono);color:var(--muted);text-align:right;
  font-variant-numeric:tabular-nums}
.acts{display:flex;gap:8px;justify-content:flex-end}
.acts a,.acts button{font:500 13px/1 var(--sans);color:var(--muted);cursor:pointer;
  text-decoration:none;background:none;border:1px solid var(--line);
  padding:6px 12px;border-radius:8px;transition:color .2s,border-color .2s,transform .1s}
.acts a:hover,.acts button:hover{color:var(--text);border-color:#333b46}
.acts a:active,.acts button:active{transform:translateY(1px)}
.empty{padding:44px 20px;text-align:center;color:var(--muted);
  border:1px dashed var(--line);border-radius:14px}
.empty svg{opacity:.35;margin-bottom:10px}

#panel{position:fixed;top:0;right:0;bottom:0;width:min(560px,94vw);z-index:20;
  background:var(--panel);border-left:1px solid var(--line);
  transform:translateX(102%);transition:transform .28s cubic-bezier(.2,.8,.25,1);
  display:flex;flex-direction:column;box-shadow:-30px 0 70px -40px #000000d9}
#panel.open{transform:none}
.phead{display:flex;align-items:center;gap:12px;padding:15px 20px;
  border-bottom:1px solid var(--line)}
.phead .t{font-weight:600;font-size:14.5px;flex:1;overflow:hidden;
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
@media (max-width:640px){
  .nrow{grid-template-columns:1fr auto;row-gap:8px}
  .ninfo{grid-column:1;text-align:left}
  .vault{display:none}
}
</style></head><body>
<header><div class="hwrap">
  <div class="brand"><span class="mark"><svg viewBox="0 0 24 24" fill="none">
    <path d="M3 8.5A2.5 2.5 0 0 1 5.5 6h3l2 2.5h8A2.5 2.5 0 0 1 21 11v6a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17z" fill="#0c0e11"/>
    <circle cx="17.5" cy="6.8" r="2.2" fill="#0c0e11"/></svg></span>
    Vision Docs</div>
  <span class="dot" title="Đang chạy"></span>
  <span class="vault" title=\"""" + html.escape(VAULT) + """">""" + html.escape(VAULT) + """</span>
</div></header>
<main>
  <div class="hero">
    <div><h1>Thả tài liệu, nhận note.</h1>
      <p>File được OCR bằng Vision ngay trên máy anh — không gửi đi đâu cả.
      Note <span class="mono">.md</span> vào Vault, sẵn sàng cho Jan.</p></div>
    <span class="count" id="count">—</span>
  </div>

  <div id="drop" tabindex="0" role="button" aria-label="Chọn hoặc thả file">
    <div class="tz"><svg width="24" height="24" viewBox="0 0 24 24" fill="none"
      stroke="#e08a5c" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <path d="M12 15V4m0 0L8 8m4-4 4 4"/><path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/></svg></div>
    <b>Thả file vào đây, hoặc bấm để chọn</b>
    <small>OCR tuần tự — file nào xong là hiện kết quả ngay bên dưới</small>
    <div class="kinds"><span class="kind">PDF</span><span class="kind">DOCX</span>
      <span class="kind">MD</span><span class="kind">TXT</span><span class="kind">PNG · JPG</span>
      <span class="kind">HEIC</span><span class="kind">TIFF</span></div>
    <input id="file" type="file" multiple hidden
       accept=".pdf,.docx,.md,.markdown,.txt,.text,.png,.jpg,.jpeg,.heic,.tiff,.webp,.bmp">
  </div>

  <div id="jobs" aria-live="polite"></div>
  <h2>Notes trong vault</h2>
  <div id="notes"></div>
</main>

<div id="scrim"></div>
<aside id="panel" aria-label="Xem trước note">
  <div class="phead"><span class="t" id="ptitle">—</span>
    <a id="pdl" class="" href="#" style="font:500 13px/1 var(--sans);color:var(--accent);
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
const drop=$('#drop'),fi=$('#file'),jobsEl=$('#jobs'),notesEl=$('#notes');
let z=0;
drop.onclick=()=>fi.click();
drop.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();fi.click();}};
['dragover','dragleave','drop'].forEach(ev=>drop.addEventListener(ev,e=>{
 e.preventDefault();drop.classList.toggle('over',ev==='dragover');
 if(ev==='drop')send(e.dataTransfer.files)}));
drop.addEventListener('mousemove',e=>{const r=drop.getBoundingClientRect();
 drop.style.setProperty('--mx',(e.clientX-r.left)+'px');
 drop.style.setProperty('--my',(e.clientY-r.top)+'px');});
fi.onchange=()=>send(fi.files);

async function send(fs){
 if(!fs||!fs.length)return;
 const fd=new FormData();[...fs].forEach(f=>fd.append('files',f));
 drop.classList.remove('over');
 let r;try{r=await(await fetch('/upload',{method:'POST',body:fd})).json();}
 catch(e){toast('Không tải lên được — server có chạy không?');return;}
 r.jobs.forEach(j=>{
  if(j.dup){return;}          /* đã có job này — không thêm card đúp */
  if(j.error){
   const d=document.createElement('div');d.className='job';
   d.innerHTML=`<span class="jstat fail" style="grid-column:1">⚠</span>
   <div><div class="jname">${j.name}</div></div>
   <span class="jstat fail">${j.error}</span>`;jobsEl.prepend(d);
   return;}
  if(!document.getElementById('j_'+j.id))addCard(j.id,j.name);});
 const allDup=r.jobs.length&&r.jobs.every(j=>j.dup);
 if(allDup)toast('File này đang được OCR rồi — theo dõi card phía trên.');
 fi.value='';}

function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;')
 .replace(/"/g,'&quot;').replace(/'/g,'&#39;')}

/* ---- jobs: change-detection — chỉ vẽ lại khi trạng thái thật sự đổi ---- */
const cards=new Map();                 // id -> last status (bộ nhớ so sánh)
function addCard(id,name){
 const d=document.createElement('div');d.className='job';d.id='j_'+id;
 d.style.animationDelay=(z++*40)+'ms';
 d.innerHTML=`<span class="jstat pend" id="s_${id}">•</span>
  <div style="min-width:0"><div class="jname">${esc(name)}</div>
  <div class="jmeta" id="m_${id}"></div></div>
  <span class="jstat pend" id="t_${id}">đang chờ</span>`;
 jobsEl.prepend(d);
 cards.set(id,'__new');}

function apply(j){
 const id=j.id;
 if(!document.getElementById('j_'+id)&&
    (j.status==='pending'||j.status==='running'))addCard(id,j.name);
 const prev=cards.get(id);
 if(!prev)return;                       // card lỗi format, không theo dõi
 if(prev===j.status)return;             /* KHÔNG đổi gì — bỏ qua, không vẽ */
 cards.set(id,j.status);
 const st=document.getElementById('s_'+id),tm=document.getElementById('t_'+id),
       mt=document.getElementById('m_'+id);
 if(j.status==='pending'){st.textContent='•';st.className='jstat pend';
  tm.className='jstat pend';tm.textContent='đang chờ';mt.textContent='';}
 if(j.status==='running'){st.innerHTML='<span class="spin"></span>';
  st.className='jstat';tm.className='jstat pend';tm.textContent='đang OCR…';
  mt.innerHTML='<div class="shimmer"></div>';}
 if(j.status==='done'){st.textContent='✓';st.className='jstat ok';
  tm.className='jstat ok';
  tm.innerHTML=`xong · ${j.seconds}s <a href="#" data-note="${esc(j.note)}">xem note</a>`;
  mt.textContent='→ '+j.note;loadNotes(true);}
 if(j.status==='failed'){st.textContent='✕';st.className='jstat fail';
  tm.className='jstat fail';tm.textContent=j.error||'lỗi';mt.textContent='';}}
jobsEl.addEventListener('click',e=>{const a=e.target.closest('[data-note]');
 if(a){e.preventDefault();openPanel(a.dataset.note);}});

let seenErr=0,dead=false;
async function poll(){
 if(dead)return;
 try{
  const js=await(await fjson('/jobs')).json();
  const ids=new Set(js.map(j=>j.id));js.forEach(apply);
  cards.forEach((_,id)=>{if(!ids.has(id)&&document.getElementById('j_'+id)){
   document.getElementById('j_'+id).remove();cards.delete(id);}});
  seenErr=0;
  try{const m=await(await fjson('/meta')).json();
   if(m.build!==BUILD){dead=true;                 /* server có bản mới */
    toast('Có phiên bản mới — đang tải lại trang…');
    setTimeout(()=>location.reload(),900);}}catch(e){}
 }catch(e){seenErr++;if(seenErr===2)toast('Mất kết nối tới server.');}}
setInterval(poll,1500);poll();

/* ---- notes: chỉ vẽ lại khi dữ liệu thật sự đổi (signature check) ---- */
let notesSig='';
function gkey(n){return n.name+'|'+n.mtime+'|'+n.kb}
async function loadNotes(force){
 let ns;try{ns=await(await fjson('/notes')).json();}catch(e){return;}
 const sig=JSON.stringify(ns.map(gkey));
 if(sig===notesSig&&!force)return;       /* không đổi — không vẽ, không giật */
 notesSig=sig;
 $('#count').innerHTML=`<b>${ns.length}</b>&nbsp;note${ns.length?
  `&nbsp;·&nbsp;cập nhật ${new Date().toLocaleTimeString('vi-VN',
   {hour:'2-digit',minute:'2-digit'})}`:''}`;
 if(!ns.length){notesEl.innerHTML=`<div class="empty">
  <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#8f99a5"
   stroke-width="1.5"><path d="M4 5a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/></svg>
  <div>Vault còn trống — thả file đầu tiên vào khung phía trên.</div></div>`;return;}
 notesEl.innerHTML=ns.map((n,i)=>`<div class="nrow" style="animation-delay:${i*30}ms">
  <span class="ftag">${n.folder}</span>
  <span class="nname"><a data-note="${esc(n.name)}" title="Xem trước">${esc(n.name)}</a></span>
  <span class="ninfo">${n.mtime} · ${n.kb} KB</span>
  <span class="acts"><a data-note="${esc(n.name)}">Xem</a>
   <a href="/download/${encodeURIComponent(n.name)}">Tải .md</a></span></div>`).join('');}
notesEl.addEventListener('click',e=>{const a=e.target.closest('[data-note]');
 if(a){e.preventDefault();openPanel(a.dataset.note);}});

/* ---- preview panel ---- */
const panel=$('#panel'),scrim=$('#scrim');
async function openPanel(name){
 $('#ptitle').textContent=name.replace(/\.md$/i,'');
 $('#pbody').textContent='Đang tải…';
 $('#pdl').href='/download/'+encodeURIComponent(name);
 panel.classList.add('open');scrim.classList.add('on');
 try{const t=await(await fetch('/note/'+encodeURIComponent(name))).text();
  $('#pbody').textContent=t;}
 catch(e){$('#pbody').textContent='Không đọc được note này.';}}
function closePanel(){panel.classList.remove('open');scrim.classList.remove('on');}
$('#px').onclick=closePanel;scrim.onclick=closePanel;
document.addEventListener('keydown',e=>{if(e.key==='Escape')closePanel();});

function toast(msg){const t=$('#toast');t.textContent=msg;t.classList.add('on');
 setTimeout(()=>t.classList.remove('on'),3200);}
loadNotes();setInterval(loadNotes,8000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE.replace("__BUILD__", BUILD)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
