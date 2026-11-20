# -*- coding: utf-8 -*-
"""Mo rong server.py: DOCX / MD / anh rieng + ingest vao Vault (Obsidian).

server.py import module nay phia cuoi va gan `server_extra.S = <module server>`
de cac helper o day goi lai pipeline OCR chung (_vision_ocr, _load_pdf, ...).
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import sys

from Foundation import NSURL, NSData
from Quartz import (
    CGImageSourceCreateWithURL,
    CGImageSourceCreateWithData,
    CGImageSourceCreateImageAtIndex,
    CGImageSourceGetCount,
)

S = None  # gan bang module server khi import

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".heic", ".heif", ".tiff", ".tif",
              ".webp", ".bmp", ".gif"}
VAULT_DEFAULT = os.environ.get("PDF_OCR_VAULT",
                              os.path.expanduser("~/Documents/Vault"))




# ---------------------------------------------------------------------------
# Resolve duong dan: model thuong chi biet TEN file (Jan inline attachment
# khong ton tai tren dia) -> tu tim theo basename.
# ---------------------------------------------------------------------------

import subprocess as _sp

SEARCH_ROOTS = [os.path.expanduser("~/Downloads"),
                os.path.expanduser("~/Desktop"),
                os.path.expanduser("~/Documents"),
                os.path.expanduser("~/Library/Application Support/Jan/data"),
                os.getcwd()]


def resolve_file(path: str, extra_roots: list[str] | None = None) -> str:
    """If `path` exists, return it (absolute). Otherwise locate the file by
    NAME (basename) in the usual folders + Spotlight (mdfind). If still not
    found, raise with a clear message so the model asks the user for a path."""
    p = os.path.abspath(os.path.expanduser(str(path).strip().strip('`"\'')))
    if os.path.isfile(p):
        return p
    base = os.path.basename(p)
    if not base:
        raise FileNotFoundError(f"Invalid path: {path!r}")

    cands: list[str] = []
    roots = SEARCH_ROOTS + list(extra_roots or [])
    # 1) tim truc tiep trong cac root
    for r in roots:
        try:
            direct = os.path.join(r, base)
            if os.path.isfile(direct):
                cands.append(direct)
        except Exception:
            pass
    # 2) Spotlight mdfind (nhanh, phu het dia may)
    if not cands:
        try:
            out = _sp.check_output(["mdfind", "-name", base], timeout=10,
                                   text=True, stderr=_sp.DEVNULL)
            cands = [l for l in out.splitlines()
                     if l and os.path.basename(l) == base and os.path.isfile(l)]
        except Exception:
            cands = []
    if cands:
        best = max(cands, key=os.path.getmtime)
        return os.path.abspath(best)
    raise FileNotFoundError(
        f"File '{base}' not found on disk. A Jan 'inline' attachment is not a "
        f"real file - provide the full path (e.g. ~/Downloads/{base}) or save "
        f"the file into Downloads/Documents.")


# ---------------------------------------------------------------------------
# Helpers anh
# ---------------------------------------------------------------------------

def _cgimage_from_file(path: str):
    url = NSURL.fileURLWithPath_(os.path.abspath(os.path.expanduser(path)))
    src = CGImageSourceCreateWithURL(url, None)
    if src is None or CGImageSourceGetCount(src) < 1:
        raise ValueError(f"Cannot read image: {path}")
    img = CGImageSourceCreateImageAtIndex(src, 0, None)
    if img is None:
        raise ValueError(f"Cannot decode image: {path}")
    return img


def _cgimage_from_bytes(blob: bytes):
    data = NSData.dataWithBytes_length_(blob, len(blob))
    src = CGImageSourceCreateWithData(data, None)
    if src is None or CGImageSourceGetCount(src) < 1:
        return None
    return CGImageSourceCreateImageAtIndex(src, 0, None)


def ocr_image_file(path: str, languages: list[str]) -> str:
    """OCR 1 file anh (png/jpg/heic/webp/tiff/...) bang Vision."""
    return S._vision_ocr(_cgimage_from_file(resolve_file(path)), languages)


# ---------------------------------------------------------------------------
# DOCX -> markdown (van ban + bang + OCR anh nhung, giu thu tu doc)
# ---------------------------------------------------------------------------

_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _docx_to_md(path: str, langs: list[str], attach_dir: str | None = None,
                 md_dir: str | None = None) -> tuple[str, int, list[str]]:
    """docx -> markdown. Embedded images are OCR'd and (if attach_dir) copied
    into the Vault. Returns (markdown, n_images, saved_files)."""
    import docx as _docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = _docx.Document(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    if attach_dir:
        os.makedirs(attach_dir, exist_ok=True)

    lines: list[str] = []
    n_img = 0
    saved_files: list[str] = []

    for el in doc.element.body.iterchildren():
        tag = el.tag
        if tag.endswith("}p"):
            para = Paragraph(el, doc)
            txt = para.text.strip()
            for blip in el.findall(f".//{_A_NS}blip"):
                rid = blip.get(f"{_R_NS}embed")
                if not rid:
                    continue
                n_img += 1
                try:
                    blob = doc.part.related_parts[rid].blob
                except Exception:
                    lines.append(f"\n> ⚠️ Ảnh {n_img} không truy xuất được\n")
                    continue
                saved = None
                if attach_dir:
                    saved = os.path.join(attach_dir, f"{stem}_img{n_img:02d}.png")
                    with open(saved, "wb") as f:
                        f.write(blob)
                    saved_files.append(saved)
                lines.append(f"\n### Ảnh {n_img}")
                if saved and md_dir:
                    lines.append(f"![ảnh {n_img}]("
                                 + os.path.relpath(saved, md_dir) + ")")
                cg = _cgimage_from_bytes(blob)
                lines.append("**OCR ảnh:** " + (S._vision_ocr(cg, langs) if cg
                                                else "*(không giải mã được)*"))
            if txt:
                style = (para.style.name or "").lower()
                if style.startswith("heading 1"):
                    lines.append(f"\n# {txt}")
                elif style.startswith("heading 2"):
                    lines.append(f"\n## {txt}")
                elif style.startswith("heading"):
                    lines.append(f"\n### {txt}")
                else:
                    lines.append(txt)
        elif tag.endswith("}tbl"):
            table = Table(el, doc)
            rows = []
            for row in table.rows:
                rows.append(" | ".join(c.text.strip().replace("\n", " ")
                                       for c in row.cells))
            if rows:
                ncols = rows[0].count("|") + 1
                lines.append("\n| " + rows[0] + " |")
                lines.append("|" + "---|" * ncols)
                for r in rows[1:]:
                    lines.append("| " + r + " |")
    return "\n".join(lines).strip(), n_img, saved_files


# ---------------------------------------------------------------------------
# MD -> markdown (van ban + OCR anh tham chieu, copy anh vao vault neu can)
# ---------------------------------------------------------------------------

_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)[^)]*\)")
_MD_WIKI_IMG_RE = re.compile(
    r"!\[\[([^\]]+\.(?:png|jpe?g|gif|webp|heic|tiff?|bmp))\]\]", re.I)


def _md_to_md(path: str, langs: list[str], attach_dir: str | None = None,
              md_dir: str | None = None) -> tuple[str, int, list[str]]:
    """Markdown -> markdown; every referenced image is OCR'd with an
    'OCR ảnh' block right after the link. If attach_dir is given, images are
    copied there and links rewritten for Obsidian."""
    src_md = open(path, "r", encoding="utf-8", errors="replace").read()
    base_dir = os.path.dirname(os.path.abspath(path))
    stem = os.path.splitext(os.path.basename(path))[0]
    if attach_dir:
        os.makedirs(attach_dir, exist_ok=True)

    out: list[str] = []
    missing: list[str] = []
    n_img = 0
    saved: list[str] = []

    def handle_img(ref: str, render: str) -> None:
        nonlocal n_img
        n_img += 1
        img_path = ref if os.path.isabs(ref) else os.path.normpath(
            os.path.join(base_dir, ref))
        if not os.path.isfile(img_path):
            missing.append(ref)
            out.append(render)
            out.append(f"> ⚠️ ảnh không tìm thấy: {ref}")
            return
        # copy sang vault (neu yeu cau) va OCR ban copy do
        src_for_ocr = img_path
        if attach_dir:
            dst = os.path.join(attach_dir, f"{stem}_img{n_img:02d}"
                               + os.path.splitext(img_path)[1].lower())
            shutil.copy2(img_path, dst)
            saved.append(dst)
            src_for_ocr = dst
            out.append(f"\n![ảnh {n_img}]("
                        + os.path.relpath(dst, md_dir or ".") + ")")
        else:
            out.append(render)
        try:
            out.append("\n> **OCR ảnh:** " + ocr_image_file(src_for_ocr, langs))
        except Exception as e:
            out.append(f"> ⚠️ OCR lỗi ({ref}): {e}")

    last = 0
    for m in _MD_IMG_RE.finditer(src_md):
        out.append(src_md[last:m.start()])
        handle_img(m.group(1), m.group(0))
        last = m.end()
    out.append(src_md[last:])

    joined = "".join(out)
    out = []
    last = 0
    for m in _MD_WIKI_IMG_RE.finditer(joined):
        out.append(joined[last:m.start()])  # bo render cu (da copy vao vault)
        handle_img(m.group(1), m.group(0))
        last = m.end()
    out.append(joined[last:])
    return "".join(out), n_img, missing


# ---------------------------------------------------------------------------
# Ingest vao Vault
# ---------------------------------------------------------------------------

def ingest_document(path: str, vault_root: str | None = None,
                    dest: str = "Inbox", languages: str | None = None,
                    dpi: float = 200.0, copy_images: bool = True) -> str:
    """Take any file (.pdf/.docx/.md/image) -> OCR -> write a .md note into
    Vault/dest (default ~/Documents/Vault/Inbox); images are copied to
    Vault/Attachments/<stem>/. Returns a JSON summary."""
    vault_root = os.path.abspath(os.path.expanduser(vault_root or VAULT_DEFAULT))
    langs = [s.strip() for s in (languages or S.DEFAULT_LANGS).split(",") if s.strip()]
    path = resolve_file(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"File not found: {path}")

    stem = os.path.splitext(os.path.basename(path))[0]
    ext = os.path.splitext(path)[1].lower()
    out_dir = os.path.join(vault_root, dest)
    os.makedirs(out_dir, exist_ok=True)
    attach_dir = os.path.join(vault_root, "Attachments", stem)
    md_path = os.path.join(out_dir, f"{stem}.md")

    if ext == ".pdf":
        doc, n, _ = S._load_pdf(path)
        first, last = S._norm_range(1, None, n)
        parts = [f"# {os.path.basename(path)}"]
        for p in range(first, last + 1):
            img, _ = S._render_page_to_cgimage(doc, p, dpi)
            parts.append(f"\n---\n## Trang {p}\n")
            parts.append(S._vision_ocr(img, langs) or "*(trang trống)*")
        body = "\n".join(parts)
        n_images = None
    elif ext == ".docx":
        body, n_images, _ = _docx_to_md(path, langs,
                                        attach_dir if copy_images else None,
                                        out_dir)
    elif ext in (".md", ".markdown"):
        body, n_images, missing = _md_to_md(path, langs,
                                           attach_dir if copy_images else None,
                                           out_dir)
    elif ext in (".txt", ".text"):
        raw = open(path, encoding="utf-8", errors="replace").read()
        body = f"# {os.path.basename(path)}\n\n{raw}"
        n_images = 0
    elif ext in IMAGE_EXTS:
        n_images = 1
        if copy_images:
            os.makedirs(attach_dir, exist_ok=True)
            shutil.copy2(path, os.path.join(attach_dir, os.path.basename(path)))
            body = (f"![ảnh]({os.path.relpath(attach_dir, out_dir)}/"
                    f"{os.path.basename(path)})\n\n")
        else:
            body = ""
        body += ocr_image_file(path, langs)
    else:
        raise ValueError(f"Unsupported file type: {ext}. Supported: "
                         ".pdf .docx .md .txt .png .jpg .heic .webp .tiff")

    front = ("---\n"
             f'source: "{os.path.basename(path)}"\n'
             f'source_path: "{path}"\n'
             f"ocr_langs: {','.join(langs)}\n"
             f"ocr_date: {datetime.date.today().isoformat()}\n"
             "---\n\n")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(front + body + "\n")

    return json.dumps({
        "ok": True,
        "written": md_path,
        "chars": len(body),
        "images_ocr": n_images,
        "type": ext.lstrip("."),
        "next": "Review the note in Vault/" + dest + " then move it to Notes/ before indexing",
    }, ensure_ascii=False, indent=1)


_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n?", re.S)


def _open_note_raw(fp: str) -> str:
    return open(fp, encoding="utf-8", errors="replace").read()


def _read_note(fp: str) -> str:
    """Read a vault note and STRIP the YAML frontmatter block.

    Frontmatter is metadata for humans/Obsidian, not searchable content:
    keeping it would (a) pollute excerpts shown to the model, and (b) create
    a phantom pseudo-page that matches keywords like `cambridge` or `pdf`.
    """
    txt = _open_note_raw(fp)
    m = _FRONTMATTER_RE.match(txt)
    if m:
        txt = txt[m.end():]
    return txt


def _vault_dirs(vault: str):
    """Yield every note file path in the vault (Notes/, Inbox/)."""
    for sub in ("Notes", "Inbox"):
        d = os.path.join(vault, sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.lower().endswith(".md"):
                yield os.path.join(d, name)


_PARA_BREAK_RE = re.compile(r"\n\n|\n#{1,6} |\n---")


def _paragraph_around(txt: str, idx: int, max_chars: int = 1500) -> str:
    """Excerpt around position `idx`, breaking at blank lines, markdown
    headings (`## Trang N`) or `---` page separators.

    OCR'd PDFs (Vision) have NO blank lines inside a page, so a naive
    paragraph split would run from the file start to the first blank line.
    """
    starts = [mm.end() for mm in _PARA_BREAK_RE.finditer(txt, 0, idx)]
    start = starts[-1] if starts else 0
    m = _PARA_BREAK_RE.search(txt, idx)
    end = m.start() if m else len(txt)
    para = " ".join(txt[start:end].split())
    if len(para) > max_chars:
        # center the window on the actual match position
        para_squished_offset = len(" ".join(txt[start:idx].split()))
        half = max_chars // 2
        if para_squished_offset > half:
            cut = para_squished_offset - half
            para = para[cut:cut + max_chars] + " [...]"
        else:
            para = para[:max_chars] + " [...]"
    return para


# ---------------------------------------------------------------------------
# Batch ingest: ca thu muc
# ---------------------------------------------------------------------------

def batch_ingest(folder: str, vault_root: str | None = None,
                 dest: str = "Inbox", languages: str | None = None,
                 dpi: float = 200.0, skip_existing: bool = True) -> str:
    """Scan a folder (recursively, but ignoring hidden dirs) and ingest every
    supported file (.pdf .docx .md .txt .png/.jpg/...). Returns a JSON summary."""
    folder = resolve_file(folder)
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Folder not found: {folder}")
    langs = languages or S.DEFAULT_LANGS
    ok, failed, skipped = [], [], []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "_done" and d != "_failed"]
        for name in sorted(files):
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in ({".pdf", ".docx", ".md", ".markdown", ".txt", ".text"} | IMAGE_EXTS):
                continue
            fp = os.path.join(root, name)
            stem = os.path.splitext(name)[0]
            note = os.path.join(vault_root or VAULT_DEFAULT, dest, stem + ".md")
            if skip_existing and os.path.isfile(note) and \
                    os.path.getmtime(note) >= os.path.getmtime(fp):
                skipped.append(fp)
                continue
            try:
                r = json.loads(ingest_document(fp, vault_root, dest, languages, dpi, True))
                ok.append({"file": fp, "written": r.get("written"), "chars": r.get("chars")})
            except Exception as e:
                failed.append({"file": fp, "error": str(e)})
    return json.dumps({"folder": folder, "ingested": len(ok), "skipped": len(skipped),
                       "failed": len(failed), "results": ok, "errors": failed},
                      ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# search_notes: keyword search tren Vault .md (da OCR san — khong ton OCR)
# ---------------------------------------------------------------------------

# Ngưỡng cosine bge-m3, hiệu chỉnh 20/11/2026 trên vault thật: query rác /
# lạc đề cao nhất 0.566 ("giá vé máy bay" vs bài travel), query có thật
# >= 0.67. Dưới SEM_LOW coi như không có.
SEM_STRONG = 0.60
SEM_LOW = 0.52


def find_note_for_source(path: str, vault_root: str | None = None) -> str | None:
    """Vault note already OCR'd from `path` (match frontmatter source_path,
    else source == basename). None if the source changed after the note."""
    vault = os.path.abspath(os.path.expanduser(vault_root or VAULT_DEFAULT))
    path = os.path.abspath(os.path.expanduser(path))
    base = os.path.basename(path)
    by_path = by_name = None
    for fp in _vault_dirs(vault):
        meta = _note_meta(fp)
        if meta.get("source_path") == path:
            by_path = fp
            break
        if by_name is None and meta.get("source") == base:
            by_name = fp
    hit = by_path or by_name
    if hit and os.path.isfile(path) and os.path.getmtime(path) > os.path.getmtime(hit):
        return None  # file gốc mới hơn note -> note có thể đã cũ
    return hit


def _note_meta(fp: str) -> dict:
    """Frontmatter key/values of a note (source, ocr_date, ...)."""
    try:
        with open(fp, encoding="utf-8", errors="replace") as f:
            head = f.read(1500)
    except OSError:
        return {}
    m = _FRONTMATTER_RE.match(head)
    meta = {}
    if m:
        for line in m.group(0).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"\'')
    return meta


_PAGE_HDR_RE = re.compile(r"^## Trang (\d+)\s*$", re.M)


def list_notes(vault_root: str | None = None) -> str:
    """Catalog of every document in the Vault (newest first)."""
    vault = os.path.abspath(os.path.expanduser(vault_root or VAULT_DEFAULT))
    rows = []
    for fp in _vault_dirs(vault):
        meta = _note_meta(fp)
        txt = _read_note(fp)
        pages = [int(x) for x in _PAGE_HDR_RE.findall(txt)]
        rows.append({
            "file": os.path.basename(fp),
            "folder": os.path.basename(os.path.dirname(fp)),
            "source": meta.get("source", ""),
            "pages": max(pages) if pages else None,
            "chars": len(txt),
            "ingested": meta.get("ocr_date", ""),
            "_mtime": os.path.getmtime(fp)})
    rows.sort(key=lambda r: -r.pop("_mtime"))
    return json.dumps({"documents": len(rows), "notes": rows,
                       "hint": "Use search_notes to find content, read_note "
                               "(file + page range) to read it in full."},
                      ensure_ascii=False, indent=1)


def _resolve_note(name: str, vault: str) -> str:
    """Note path inside the vault from a full path, file name or part of it.
    Never reads outside the vault."""
    notes = list(_vault_dirs(vault))
    want = os.path.abspath(os.path.expanduser(name.strip()))
    if want in notes:
        return want
    base = os.path.basename(name.strip())
    exact = [fp for fp in notes if os.path.basename(fp) == base
             or os.path.basename(fp) == base + ".md"
             or os.path.splitext(os.path.basename(fp))[0] + ".pdf" == base]
    if len(exact) == 1:
        return exact[0]
    key = os.path.splitext(base)[0].lower()
    part = [fp for fp in notes if key and key in os.path.basename(fp).lower()]
    if len(part) == 1:
        return part[0]
    cands = [os.path.basename(fp) for fp in (exact or part)][:10]
    if cands:
        raise ValueError(f"'{name}' matches several notes: {cands} - "
                         "pass the exact file name.")
    raise FileNotFoundError(f"No vault note named '{name}'. Call list_notes "
                            "to see the available documents.")


READ_NOTE_MAX = 12_000


def read_note(file: str, first_page: int = 1, last_page: int | None = None,
              vault_root: str | None = None) -> str:
    """Full text of pages first_page..last_page of a vault note (no OCR).
    Notes without page markers are read in chunks of READ_NOTE_MAX chars
    (first_page = chunk number)."""
    vault = os.path.abspath(os.path.expanduser(vault_root or VAULT_DEFAULT))
    fp = _resolve_note(file, vault)
    txt = _read_note(fp)
    marks = list(_PAGE_HDR_RE.finditer(txt))
    first = max(1, int(first_page or 1))
    if marks:
        total = int(marks[-1].group(1))
        last = min(int(last_page or first), total)
        parts = []
        for k, mm in enumerate(marks):
            pno = int(mm.group(1))
            if first <= pno <= last:
                end = marks[k + 1].start() if k + 1 < len(marks) else len(txt)
                body = txt[mm.end():end].strip().rstrip("-").strip()
                parts.append(f"## Trang {pno}\n{body}")
        out = "\n\n".join(parts)
        unit = "page"
    else:
        total = max(1, -(-len(txt) // READ_NOTE_MAX))
        last = min(int(last_page or first), total)
        out = txt[(first - 1) * READ_NOTE_MAX:last * READ_NOTE_MAX]
        unit = "chunk"
    if first > total:
        return json.dumps({"file": os.path.basename(fp), "error":
                           f"{unit} {first} out of range (1-{total})"},
                          ensure_ascii=False)
    truncated = len(out) > READ_NOTE_MAX
    res = {"file": os.path.basename(fp), "source": _note_meta(fp).get("source", ""),
           f"{unit}s": f"{first}-{last} of {total}",
           "text": out[:READ_NOTE_MAX]}
    if truncated:
        res["note"] = (f"Output cut at {READ_NOTE_MAX} chars - read fewer "
                       f"{unit}s at a time.")
    return json.dumps(res, ensure_ascii=False, indent=1)


def search_notes(query: str, vault_root: str | None = None,
                 max_hits: int = 6, only_file: str | None = None,
                 pages: tuple[int, int] | None = None) -> str:
    """Search across Vault .md notes (Inbox/ + Notes/, skip Attachments).
      1) EXACT PHRASE: whole query as substring - try first, return if hit.
      2) SEMANTIC (bge-m3) + KEYWORDS RANK (per page, stopwords removed).
         Strong semantic (>=SEM_STRONG) wins; weak (>=SEM_LOW) wins only
         when keyword overlap < 50%; otherwise keyword pages are returned.
         Embed server is always waited for (prewarmed at MCP startup).
    only_file / pages: restrict to one note and a page range (search_pdf).
    Returns mode, file, page, excerpt per hit (+ semantic_error on failure).
    """
    import re as _re
    vault = os.path.abspath(os.path.expanduser(vault_root or VAULT_DEFAULT))
    q = query.strip().lower()
    if not q:
        return "Empty query."

    def _in_range(pg) -> bool:
        if not pages:
            return True
        try:
            return pages[0] <= int(pg) <= pages[1]
        except (TypeError, ValueError):
            return False

    # ---------- mode 1: exact phrase ----------
    hits = []
    for sub in ("Notes", "Inbox"):
        d = os.path.join(vault, sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.lower().endswith(".md"):
                continue
            fp = os.path.join(d, name)
            if only_file and fp != only_file:
                continue
            txt = _read_note(fp)
            low = txt.lower()
            idx = low.find(q)
            while idx != -1 and len(hits) < max_hits:
                page = ""
                m = list(_re.finditer(r"^## Trang (\d+)\s*$", txt[:idx], _re.M))
                if m:
                    page = m[-1].group(1)
                if _in_range(page):
                    para = _paragraph_around(txt, idx, 700)
                    hits.append({"file": fp, "page": page, "excerpt": para})
                idx = low.find(q, idx + len(q))
    if hits:
        return json.dumps({"query": query, "mode": "exact-phrase",
                           "matches": len(hits), "results": hits},
                          ensure_ascii=False, indent=1)

    # ---------- mode 2: per-page keyword ranking ----------
    STOP = {"and", "for", "the", "with", "from", "và", "của", "cho", "trong",
            "các", "có", "được", "là", "về", "một", "này", "that", "this",
            "then", "into", "tôi", "anh", "em", "you", "your"}
    _TOK = r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ\w]{3,}|\d[\d.,%]*"
    words = [w for w in _re.findall(_TOK, q)
             if w not in STOP]
    _PAGE_RE = _re.compile(r"^## Trang (\d+)\s*$", _re.M)
    scored = []  # (distinct, total, file, page, first_idx, seg_txt)
    for sub in (("Notes", "Inbox") if words else ()):
        d = os.path.join(vault, sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.lower().endswith(".md"):
                continue
            fp = os.path.join(d, name)
            if only_file and fp != only_file:
                continue
            txt = _read_note(fp)
            marks = list(_PAGE_RE.finditer(txt))
            if marks:
                bounds = [(0, marks[0].start(), "")]
                for k, mm in enumerate(marks):
                    end = marks[k + 1].start() if k + 1 < len(marks) else len(txt)
                    bounds.append((mm.end(), end, mm.group(1)))
            else:
                bounds = [(0, len(txt), "")]
            for a, b, pno in bounds:
                seg = txt[a:b].lower()
                if not seg.strip():
                    continue
                words_present = [w for w in words if w in seg]
                # Phải khớp >= 60% số từ khoá (tối thiểu 1). Âm tiết tiếng Việt
                # ngắn ("công", "giá") khớp substring khắp nơi, nên trùng 1-2
                # từ gần như không mang thông tin.
                if len(words_present) < max(1, -(-len(words) * 3 // 5)):
                    continue
                if not _in_range(pno):
                    continue
                total = sum(seg.count(w) for w in words_present)
                scored.append((len(words_present), total, fp, pno, a, txt[a:b],
                                words_present))
    scored.sort(key=lambda r: (-r[0], -r[1]))

    # TIER 3(a): thử semantic trước khi trả keyword. Lý do: (1) model nhỏ
    # (Jan 4B) hay tự rút gọn câu hỏi thành 2-3 từ; (2) keyword mode match
    # substring nên dễ ăn point oan ("bắt" + "bắt đầu"); (3) vault đa ngôn
    # ngữ nên VI<->EN phải đi qua bge-m3. LUÔN chờ embed server (MCP server
    # đã prewarm lúc khởi động): câu tiếng Việt dài gần như luôn có keyword
    # hit rác (âm tiết phổ biến), nên "keyword có kết quả" KHÔNG phải lý do
    # để bỏ semantic.
    _kw_ratio = (scored[0][0] / len(words)) if scored else 0.0
    _sem, _sem_err = None, None
    try:
        _sem = _semantic_search_notes(query, vault,
                                      top_k=max(1, min(max_hits, 6)),
                                      thresh=SEM_LOW, only_file=only_file)
        if _sem and pages:
            _sem = [r for r in _sem if _in_range(r.get("page"))] or None
    except Exception as e:  # embed server lỗi / timeout / thiếu model
        _sem_err = f"{type(e).__name__}: {e}"
        print(f"[search_notes] semantic search failed: {_sem_err}",
              file=sys.stderr)
    _top = _sem[0].get("score", 0) if _sem else 0.0

    def _out(payload: dict) -> str:
        payload = {"query": query, **payload}
        if _sem_err:
            payload["semantic_error"] = _sem_err
        return json.dumps(payload, ensure_ascii=False, indent=1)

    if _top >= SEM_STRONG:
        return _out({"mode": "semantic",
                     "note": ("SEMANTIC results, cross-language, ranked by "
                              "meaning (best first). Each result gives file, "
                              "page and score."),
                     "matches": len(_sem), "results": _sem})
    # Semantic yếu: chỉ ưu tiên khi keyword cũng yếu (< nửa số từ khoá).
    if _top >= SEM_LOW and _kw_ratio < 0.5:
        return _out({"mode": "semantic-low",
                     "note": ("LOW-CONFIDENCE semantic matches (scores are "
                              "modest; the content may only be loosely "
                              "related). Cross-language, ranked by meaning. "
                              "If nothing below is relevant, the content is "
                              "probably not in the vault. Each result gives "
                              "file and page."),
                     "matches": len(_sem), "results": _sem})

    results = []
    for distinct, total, fp, pno, a, seg, wp in scored[:max(1, max_hits)]:
        paragraphs = [p.strip() for p in seg.split("\n\n") if p.strip()]
        best_paras = [p[:500] for p in paragraphs
                      if any(w in p.lower() for w in wp)][:2]
        if not best_paras:
            best_paras = [" ".join(seg.split())[:500]]
        results.append({"file": fp, "page": pno,
                        "matched_keywords": wp,
                        "keyword_score": f"{distinct}/{len(words)} words",
                        "excerpt": "\n\n".join(best_paras)})
    if results:
        payload = {"mode": "keywords-ranked",
                   "note": ("exact phrase not found - ranked best PAGES by "
                            "keyword overlap"),
                   "keywords": words, "matches": len(results),
                   "results": results}
        if _sem:  # semantic-low nhưng keyword mạnh hơn -> đính kèm tham khảo
            payload["semantic_low_results"] = _sem
        return _out(payload)

    if _sem_err:
        hint = ("Keyword search found nothing and semantic search FAILED "
                "(see semantic_error) - absence is NOT confirmed.")
    elif not words:
        hint = ("No usable keywords in query and semantic search found "
                "nothing above threshold.")
    else:
        hint = ("Not found by phrase, keywords or semantic search "
                "(above threshold). The content is very likely absent "
                "from the vault notes.")
    return _out({"mode": "keywords+semantic", "matches": 0, "results": [],
                 "hint": hint})


# ---------------------------------------------------------------------------
# Tier 3: SEMANTIC search (bge-m3 via llama.cpp) — cross-language VI<->EN
# ---------------------------------------------------------------------------

import json as _json
import math as _math
import re as _re
import subprocess as _sp2
import time as _time
import urllib.request as _url
import urllib.error as _urlerr

_ES_PORT = int(os.environ.get("PDF_OCR_EMB_PORT", "19581"))
_ES_URL = f"http://127.0.0.1:{_ES_PORT}"
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_EMB_MODEL_FILENAME = "bge-m3-Q8_0.gguf"


def _find_emb_model() -> str:
    """Locate the bge-m3 GGUF embedding model, portable across machines.

    Priority: PDF_OCR_EMB_MODEL env -> <project>/models/ (shipped or
    downloaded by install.sh) -> ~/.local/share/jan-vision-docs/ -> legacy:
    the GGUF overlaid inside Jan's `sentence-transformer-mini` dir.
    Returns "" when no model exists (semantic tier degrades gracefully).
    """
    candidates = [
        os.environ.get("PDF_OCR_EMB_MODEL", ""),
        os.path.join(_THIS_DIR, "models", _EMB_MODEL_FILENAME),
        os.path.expanduser(f"~/.local/share/jan-vision-docs/{_EMB_MODEL_FILENAME}"),
        os.path.expanduser(
            "~/Library/Application Support/Jan/data/llamacpp/models/"
            "sentence-transformer-mini/model.gguf"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return ""


_BGE_GGUF = _find_emb_model()
_LLAMA_SERVER = (os.environ.get("PDF_OCR_LLAMA_SERVER")
                 or shutil.which("llama-server")
                 or "/opt/homebrew/bin/llama-server")
_EMB_CACHE_DIR = os.environ.get("PDF_OCR_CACHE_DIR", "/tmp/pdfocr_embed_cache")


def _es_health() -> bool:
    try:
        with _url.urlopen(f"{_ES_URL}/health", timeout=3) as r:
            return b"ok" in r.read()
    except Exception:
        return False


_ES_PROC = None  # llama-server do process này spawn (tránh spawn trùng)


def _ensure_embed_server(timeout_s: int = 90, wait: bool = True) -> bool:
    """Bảo đảm embed server chạy. wait=False: nếu chưa sẵn sàng thì khởi
    động ở nền (nếu chưa khởi động) rồi trả False ngay, không chặn."""
    global _ES_PROC
    if _es_health():
        return True
    if not os.path.isfile(_BGE_GGUF) or not os.path.isfile(_LLAMA_SERVER):
        return False
    if _ES_PROC is None or _ES_PROC.poll() is not None:
        os.makedirs(_EMB_CACHE_DIR, exist_ok=True)
        log = open(os.path.join(_EMB_CACHE_DIR, "llama-server.log"), "ab")
        _ES_PROC = _sp2.Popen(
            [_LLAMA_SERVER, "-m", _BGE_GGUF, "--embeddings",
             "--pooling", "cls", "-c", "2048", "-b", "2048", "-ub", "2048",
             "-t", "4", "--host", "127.0.0.1", "--port", str(_ES_PORT)],
            stdout=log, stderr=_sp2.STDOUT, start_new_session=True)
    if not wait:
        return False
    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        if _es_health():
            return True
        _time.sleep(2)
    return False


def _embed_texts(texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    BATCH = 16
    for i in range(0, len(texts), BATCH):
        batch = [t[:4000] for t in texts[i:i + BATCH]]
        req = _url.Request(
            f"{_ES_URL}/v1/embeddings",
            data=_json.dumps({"input": batch}).encode(),
            headers={"Content-Type": "application/json"})
        with _url.urlopen(req, timeout=300) as r:
            d = _json.loads(r.read())
        d_sorted = sorted(d["data"], key=lambda x: x["index"])
        out.extend(item["embedding"] for item in d_sorted)
    return out


def _vault_page_vectors(vault: str) -> dict[int, list]:
    """Embed tung TRANG cua moi note vault (cache theo mtime). Tra ve
    {file_path: {"mtime":..., "pages":[(page_no, page_text, vector), ...]}}"""
    os.makedirs(_EMB_CACHE_DIR, exist_ok=True)
    cache_fp = os.path.join(_EMB_CACHE_DIR, "vault_index.json")
    cache: dict = {}
    if os.path.isfile(cache_fp):
        try:
            cache = _json.load(open(cache_fp))
        except Exception:
            cache = {}

    page_re = _re.compile(r"^## Trang (\d+)\s*$", _re.M)
    for sub in ("Notes", "Inbox"):
        d = os.path.join(vault, sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.lower().endswith(".md"):
                continue
            fp = os.path.join(d, name)
            mtime = os.path.getmtime(fp)
            ent = cache.get(fp)
            if ent and ent.get("mtime") == mtime:
                continue  # cache con崭 nguyên
            txt = _read_note(fp)
            marks = list(page_re.finditer(txt))
            segs: list[tuple[str, str]] = []
            if marks:
                pre = txt[:marks[0].start()].strip()
                if pre:
                    segs.append(("", pre))
                for k, mm in enumerate(marks):
                    end = marks[k + 1].start() if k + 1 < len(marks) else len(txt)
                    segs.append((mm.group(1), txt[mm.end():end].strip()))
            else:
                segs.append(("", txt.strip()))
            segs = [(p, t) for p, t in segs if len(t) >= 30]
            vectors = _embed_texts([t for _, t in segs])
            cache[fp] = {"mtime": mtime,
                         "pages": [{"p": p, "t": t, "v": [round(x, 4) for x in v]}
                                   for (p, t), v in zip(segs, vectors)]}
    # note đã xoá/di chuyển -> bỏ vector cũ, kẻo search_notes trả kết quả
    # từ tài liệu không còn tồn tại
    for fp in [k for k in cache if not os.path.isfile(k)]:
        del cache[fp]
    tmp = cache_fp + ".tmp"
    _json.dump(cache, open(tmp, "w"))
    os.replace(tmp, cache_fp)
    return cache


def _cosine(a, b) -> float:
    na = _math.sqrt(sum(x * x for x in a))
    nb = _math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def _windows(text: str, max_chars: int = 800) -> list[str]:
    """Cat trang OCR thanh cua so chuoi dong, ~max_chars, gian lan 2 dong."""
    lines = text.split("\n")
    wins, cur = [], ""
    i = 0
    while i < len(lines):
        chunk = "\n".join(lines[i:i + 4])
        if not chunk.strip():
            i += 4
            continue
        wins.append(chunk)
        i += 3  # gian lan 2 dong
    return [w for w in wins if len(w.strip()) >= 60]


def _semantic_search_notes(query: str, vault: str,
                           top_k: int = 6, thresh: float = SEM_LOW,
                           wait_for_server: bool = True,
                           only_file: str | None = None):
    """Hai chang:
      1) rank TRANG bang embedding (cache);
      2) ZOOM-IN: voi top trang, embed tung cua so ~4 dong va tra ve cua so
         khop nghia nhat. Excerpt do chinh xac hon cau lap.
    Tra ve list {file, page, score, excerpt} hoac None."""
    if not _ensure_embed_server(wait=wait_for_server):
        return None
    qv = _embed_texts([query])[0]
    cache = _vault_page_vectors(vault)
    pages = []
    for fp, ent in cache.items():
        if only_file and fp != only_file:
            continue
        for pg in ent.get("pages", []):
            score = _cosine(qv, pg["v"])
            pages.append((score, fp, pg.get("p", ""), pg.get("t", "")))
    pages.sort(key=lambda r: -r[0])
    cand = [r for r in pages[:10] if r[0] >= 0.30]
    if not cand:
        return None
    # ZOOM: embed tat ca cua so cua cac ung vien trang
    win_list = []   # (file, page, window_text)
    for score, fp, p, t in cand:
        for w in _windows(t):
            win_list.append((fp, p, w))
    if not win_list:
        return None
    wvecs = _embed_texts([w[:1400] for _, _, w in win_list])
    scored = []
    for (fp, p, w), v in zip(win_list, wvecs):
        ws = _cosine(qv, v)
        if ws >= thresh:
            scored.append((ws, fp, p, w))
    scored.sort(key=lambda r: -r[0])
    results = []
    per_page: dict = {}
    for ws, fp, p, w in scored:
        n_this_page = per_page.get((fp, p), 0)
        if n_this_page >= 2:          # toi da 2 cua so chien nguoc cua 1 trang
            continue
        per_page[(fp, p)] = n_this_page + 1
        results.append({"file": fp, "page": p, "score": round(ws, 3),
                        "excerpt": (" ".join(w.split()))[:900]})
        if len(results) >= top_k:
            break
    return results or None
