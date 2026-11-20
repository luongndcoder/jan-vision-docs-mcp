#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jan-vision-docs-mcp — Local MCP server cho Jan trên macOS (Vision OCR).

Doc file PDF (ke ca ban scan, anh chup) bang AI OCR chay 100% local tren Mac
(Apple Vision framework - Live Text, ho tro tieng Viet `vi-VT`).

Tools:
  - pdf_info    : so trang, kich thuoc, trang nao co text layer san
  - ocr_pdf     : OCR toan bo/khoang trang -> markdown; co the kem anh trang
                  (ImageContent cho model vision) va/hoac xuat file .md
  - search_pdf  : OCR roi tu khoa, tra ve doan co match (tiet kiem context)
  - export_page_images : xuat trang PDF ra file JPG/PNG (keo tha vo Jan chat)

Chay:  python server.py            (stdio, danh cho Jan cau hinh MCP)
Test nhanh khong can MCP:  python server.py --selftest file.pdf
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
from typing import Any

import Vision
from Foundation import NSURL
from Quartz import (
    CGPDFDocumentCreateWithURL,
    CGPDFDocumentGetNumberOfPages,
    CGPDFDocumentGetPage,
    CGPDFPageGetBoxRect,
    kCGPDFMediaBox,
    CGBitmapContextCreate,
    CGColorSpaceCreateDeviceRGB,
    CGContextScaleCTM,
    CGContextTranslateCTM,
    CGContextSetRGBFillColor,
    CGContextFillRect,
    CGContextDrawPDFPage,
    CGRectMake,
    CGBitmapContextCreateImage,
    CGImageDestinationCreateWithData,
    CGImageDestinationAddImage,
    CGImageDestinationFinalize,
    kCGImageAlphaPremultipliedLast,
    kCGBitmapByteOrder32Big,
    kCGImageDestinationLossyCompressionQuality,
    NSMutableData,
)
from CoreServices import kUTTypeJPEG, kUTTypePNG
from mcp.server.fastmcp import FastMCP
import pypdf

DEFAULT_LANGS = os.environ.get("PDF_OCR_LANGS", "vi-VN,en-US")
MCP = FastMCP(
    "vision-docs",
    instructions=(
        "Local, offline document research over the user's Vault (documents "
        "OCR'd on this Mac). RESEARCH WORKFLOW: (1) list_notes - see which "
        "documents exist (never guess a file name); (2) search_notes - find "
        "WHERE something is said; ask in any language, it matches by meaning "
        "across Vietnamese/English; (3) read_note - read the full page(s) "
        "around a hit before answering or summarising; (4) answer with the "
        "document name and PAGE NUMBER taken from the tool results. "
        "Jan project RAG chunks may also appear in the conversation: fine "
        "for general context, but they carry no page numbers - never invent "
        "one; confirm with search_notes before quoting, and before saying "
        "something is NOT in the documents. "
        "search_pdf: only for one PDF file the user named (uses its Vault "
        "note when ingested). ingest_document: add a new file (pdf/docx/md/"
        "image) to the Vault. Chat attachments are not files on disk - pass "
        "the file name and the server locates it."
    ),
)

MAX_INLINE_TEXT_CHARS = 30_000  # qua day se cat va ghi ban day du ra file .md

# Model nhỏ (Jan 4B) chọn tool kém khi có nhiều tool -> mặc định chỉ đăng ký
# bộ research gọn (list_notes, search_notes, read_note, search_pdf,
# ingest_document). VISION_DOCS_ALL_TOOLS=1 bật lại các tool OCR/ảnh phụ
# (hữu ích cho model vision / người dùng nâng cao).
ALL_TOOLS = os.environ.get("VISION_DOCS_ALL_TOOLS", "").lower() in ("1", "true", "yes")


def _extra_tool(fn):
    return MCP.tool()(fn) if ALL_TOOLS else fn


# ---------------------------------------------------------------------------
# Quartz / Vision helpers
# ---------------------------------------------------------------------------

def _load_pdf(path: str):
    """Mo PDF, tra ve (document, num_pages, resolved_path)."""
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    if not os.path.isfile(path):
        # path model dua ra thuong la ten file (Jan inline khong ton tai tren
        # dia) -> tu resolve theo basename qua server_extra.resolve_file
        import server_extra as _se
        path = _se.resolve_file(path)
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise FileNotFoundError(f"File not found: {path}")
    if not path.lower().endswith(".pdf"):
        raise ValueError(f"Not a PDF file: {path}")
    doc = CGPDFDocumentCreateWithURL(NSURL.fileURLWithPath_(path))
    if doc is None:
        raise ValueError(f"Cannot open PDF (corrupt or password-protected?): {path}")
    import Quartz as _Q
    if _Q.CGPDFDocumentIsEncrypted(doc) and not _Q.CGPDFDocumentIsUnlocked(doc) \
            and not _Q.CGPDFDocumentUnlockWithPassword(doc, b""):
        raise ValueError(
            f"PDF is password-protected: {os.path.basename(path)}. Ask the "
            "user to open it in Preview, enter the password and Export as PDF "
            "(without password), then use the new file.")
    n = CGPDFDocumentGetNumberOfPages(doc)
    if n == 0:
        raise ValueError(f"Empty PDF: {path}")
    return doc, n, path


def _render_page_to_cgimage(doc, page_no: int, dpi: float):
    """Render 1 trang PDF thanh CGImage bitmap RGB nen trang."""
    page = CGPDFDocumentGetPage(doc, page_no)
    if page is None:
        raise ValueError(f"Page {page_no} does not exist")
    box = CGPDFPageGetBoxRect(page, kCGPDFMediaBox)
    scale = max(72.0, min(float(dpi), 400.0)) / 72.0
    w = max(8, int(box.size.width * scale))
    h = max(8, int(box.size.height * scale))

    cs = CGColorSpaceCreateDeviceRGB()
    ctx = CGBitmapContextCreate(None, w, h, 8, w * 4, cs,
                                kCGImageAlphaPremultipliedLast | kCGBitmapByteOrder32Big)
    CGContextSetRGBFillColor(ctx, 1.0, 1.0, 1.0, 1.0)
    CGContextFillRect(ctx, CGRectMake(0, 0, w, h))
    CGContextScaleCTM(ctx, scale, scale)
    CGContextTranslateCTM(ctx, -box.origin.x, -box.origin.y)
    CGContextDrawPDFPage(ctx, page)
    return CGBitmapContextCreateImage(ctx), (w, h)


def _vision_ocr(cgimage, languages: list[str]) -> str:
    """OCR 1 anh (CGImage) bang Vision framework, text xep theo reading order."""
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(languages)
    req.setUsesLanguageCorrection_(True)

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cgimage, None)
    ok, err = handler.performRequests_error_([req], None)
    if not ok and err is not None:
        raise RuntimeError(f"Vision OCR error: {err}")

    items = []
    for o in (req.results() or []):
        cand = o.topCandidates_(1)
        if not cand:
            continue
        bb = o.boundingBox()
        items.append({"text": cand[0].string(), "x": bb.origin.x,
                      "y": bb.origin.y, "h": bb.size.height})
    # Vision bounding-box goc toa do o goc trai-DUOI: sort nhu nguoi doc
    items.sort(key=lambda r: (-r["y"], r["x"]))
    lines: list[str] = []
    buf: list[str] = []
    line_y = None
    for r in items:
        if line_y is None or abs(r["y"] - line_y) <= max(0.006, r["h"] * 0.6):
            buf.append(r["text"])
            line_y = r["y"] if line_y is None else line_y
        else:
            lines.append(" ".join(buf))
            buf = [r["text"]]
            line_y = r["y"]
    if buf:
        lines.append(" ".join(buf))
    return "\n".join(lines).strip()


def _write_cgimage(cgimage, file_path: str, jpeg_quality: float = 0.8) -> str:
    """CGImage -> file JPG/PNG qua CGImageDestination (ghi qua file tam cho chac)."""
    ext = os.path.splitext(file_path)[1].lower() or ".jpg"
    utype = kUTTypeJPEG if ext in (".jpg", ".jpeg") else kUTTypePNG
    data = NSMutableData.data()
    dest = CGImageDestinationCreateWithData(data, utype, 1, None)
    if dest is None:
        raise RuntimeError("Could not create image destination")
    props = {kCGImageDestinationLossyCompressionQuality: jpeg_quality} if utype == kUTTypeJPEG else None
    CGImageDestinationAddImage(dest, cgimage, props)
    if not CGImageDestinationFinalize(dest):
        raise RuntimeError("Could not encode image")
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
        tmp = tf.name
    data.writeToFile_atomically_(tmp, True)
    os.replace(tmp, file_path)
    return file_path


def _has_text_layer(path: str, max_pages: int = 30) -> dict[int, bool]:
    """Trang nao co text layer that (copy duoc)? - sample toi da max_pages trang."""
    # pypdf chỉ để đoán text layer — lỗi ở đây (PDF mã hoá, hỏng) không được
    # làm hỏng cả tool; Quartz vẫn render/OCR được nhiều PDF mà pypdf chịu.
    try:
        reader = pypdf.PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            return {}
        pages = reader.pages[:max_pages]
    except Exception:
        return {}
    has: dict[int, bool] = {}
    for i, page in enumerate(pages, start=1):
        try:
            txt = (page.extract_text() or "").strip()
        except Exception:
            txt = ""
        has[i] = len(txt) >= 20
    return has


def _norm_range(first: int, last: int | None, n: int) -> tuple[int, int]:
    first = max(1, int(first))
    last = n if last is None else min(int(last), n)
    if last < first:
        first, last = last, first
    return first, last


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@_extra_tool
def pdf_info(path: str) -> str:
    """PDF overview: page count, sizes, which pages have a text layer
    (copyable) vs scanned/image pages (need OCR). Call this before OCR.
    `path` must be a path ON DISK (a chat attachment name is not a file) - but
    if you only know the file name, pass the name and the server will search
    Downloads/Documents/etc. for it."""
    doc, n, resolved = _load_pdf(path)
    text_pages = _has_text_layer(resolved)
    dims = []
    for p in range(1, n + 1):
        box = CGPDFPageGetBoxRect(CGPDFDocumentGetPage(doc, p), kCGPDFMediaBox)
        dims.append({"page": p, "width_pt": round(box.size.width, 1),
                     "height_pt": round(box.size.height, 1)})
    need_ocr = [d["page"] for d in dims if not text_pages.get(d["page"], False)]
    summary = {
        "file": resolved,
        "size_mb": round(os.path.getsize(resolved) / 1_048_576, 2),
        "pages": n,
        "pages_with_text_layer": sorted(k for k, v in text_pages.items() if v),
        "pages_needing_ocr": need_ocr,
        "note": "Some pages are scans/images - use ocr_pdf for those." if need_ocr
                else "All pages have a text layer - no OCR needed.",
    }
    if n <= 60:
        summary["page_sizes"] = dims
    return json.dumps(summary, ensure_ascii=False, indent=1)


@_extra_tool
def ocr_pdf(
    path: str,
    first_page: int = 1,
    last_page: int | None = None,
    languages: str = DEFAULT_LANGS,
    output_md_path: str | None = None,
    include_images: bool = False,
) -> Any:
    """OCR a PDF with the Mac's local AI OCR (Apple Vision); works on scanned
    pages too.

    USAGE RULES:
    - If you only need to FIND where some content is mentioned, use
      `search_pdf` instead of this tool.
    - PDF ranges over 25 pages: this tool automatically writes the full text
      to a .md file and returns a 2-page preview + the file path. Read or grep
      that file for follow-up - do NOT call ocr_pdf again.
    - Only OCR small ranges (e.g. 10-30 pages) when you need the text inline.
    - If the PDF is already in the Vault, use search_notes / search_pdf
      instead of re-OCR-ing it.
    `languages`: keep the default unless the document is not Vietnamese or
    English (e.g. "zh-Hans" for Chinese). include_images=true only for
    vision-capable models (max 5 page images).
    """
    dpi, max_images = 200.0, 5
    doc, n, resolved = _load_pdf(path)
    first, last = _norm_range(first_page, last_page, n)
    langs = [s.strip() for s in languages.split(",") if s.strip()] or DEFAULT_LANGS.split(",")

    # GUARD: tai lieu dai > 25 trang -> luon ghi ra file .md (tra ve preview + duong dan)
    # de tool output khong bi Jan cut specific o ~28-30k ky tu
    if (last - first + 1) > 25 and not output_md_path and not include_images:
        output_md_path = (os.path.splitext(resolved)[0]
                          + f"_ocr_p{first}-{last}.md")

    pages_text: dict[int, str] = {}
    for p in range(first, last + 1):
        img, _ = _render_page_to_cgimage(doc, p, dpi)
        pages_text[p] = _vision_ocr(img, langs) or "*(blank page / no text detected)*"

    md = [f"# OCR: {os.path.basename(resolved)}",
          f"_Trang {first}-{last}/{n} | {dpi} DPI | langs={','.join(langs)}_"]
    for p in range(first, last + 1):
        md.append(f"\n---\n## Trang {p}\n")
        md.append(pages_text[p])
    full_md = "\n".join(md)

    # xuat file anh trang neu can kem anh
    image_files: list[str] = []
    if include_images:
        out_dir = (os.path.dirname(os.path.abspath(os.path.expanduser(output_md_path)))
                   if output_md_path else tempfile.mkdtemp(prefix="pdf_ocr_"))
        os.makedirs(out_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(resolved))[0]
        for p in range(first, last + 1):
            img, _ = _render_page_to_cgimage(doc, p, dpi)
            image_files.append(_write_cgimage(img, os.path.join(out_dir, f"{stem}_p{p:04d}.jpg")))

    content: list[dict[str, Any]] = []
    if output_md_path:
        out_abs = os.path.abspath(os.path.expanduser(output_md_path))
        os.makedirs(os.path.dirname(out_abs), exist_ok=True)
        with open(out_abs, "w", encoding="utf-8") as f:
            f.write(full_md)
        preview = "\n\n".join(f"### Trang {p}\n" + pages_text[p][:900]
                              for p in range(first, min(last, first + 1) + 1))
        content.append({
            "type": "text",
            "text": (f"OCR done for {last - first + 1} pages; full text "
                     f"({len(full_md)} chars) written to `{out_abs}`. "
                     "Preview:\n\n" + preview),
        })
    elif len(full_md) > MAX_INLINE_TEXT_CHARS:
        side = os.path.splitext(resolved)[0] + f"_ocr_p{first}-{last}.md"
        with open(side, "w", encoding="utf-8") as f:
            f.write(full_md)
        content.append({
            "type": "text",
            "text": (f"Output too long ({len(full_md)} chars) - full text at "
                     f"`{side}`. First part:\n\n"
                     + full_md[:MAX_INLINE_TEXT_CHARS] + "\n...(truncated)"),
        })
    else:
        content.append({"type": "text", "text": full_md})

    if include_images:
        for fp in image_files[: max(1, min(max_images, 10))]:
            with open(fp, "rb") as f:
                content.append({"type": "image", "data": base64.b64encode(f.read()).decode("ascii"),
                                "mimeType": "image/jpeg"})

    return content[0]["text"] if len(content) == 1 else content


@MCP.tool()
def search_pdf(
    path: str,
    query: str,
    first_page: int = 1,
    last_page: int | None = None,
) -> str:
    """Find where `query` appears in ONE PDF FILE THE USER NAMED - returns
    PAGE + surrounding text. If the PDF was already ingested into the Vault
    its OCR note is searched instantly (semantic, cross-language - a
    Vietnamese sentence finds English text); otherwise the PDF is OCR'd now
    (~0.5 s/page) and searched for the literal text.
    If the user did not name a PDF file, use search_notes instead. If `path`
    cannot be found, this tool automatically searches all Vault notes."""
    try:
        doc, n, resolved = _load_pdf(path)
    except FileNotFoundError:
        # Model nhỏ hay bịa tên file ("document.pdf") — thay vì trả lỗi khiến
        # nó đi lạc, tìm luôn trong các note Vault đã OCR.
        res = json.loads(server_extra.search_notes(query))
        res["note"] = (f"File {path!r} does not exist - searched the Vault "
                       "notes instead (search_notes). " + res.get("note", ""))
        return json.dumps(res, ensure_ascii=False, indent=1)
    first, last = _norm_range(first_page, last_page, n)
    q = query.strip().lower()
    if not q:
        return "Empty query."

    # Đã có note OCR của file này trong Vault -> tìm trong note, khỏi OCR lại
    # (IELTS 178 trang: ~97 s mỗi lần gọi -> ~0 s).
    note = server_extra.find_note_for_source(resolved)
    if note:
        rng = None if (first, last) == (1, n) else (first, last)
        res = json.loads(server_extra.search_notes(query, None, 6,
                                                   only_file=note, pages=rng))
        res["note"] = (f"Searched the existing OCR note of this PDF ({n} pages, "
                       "no re-OCR). " + res.get("note", ""))
        return json.dumps(res, ensure_ascii=False, indent=1)

    langs = DEFAULT_LANGS.split(",")
    hits: list[dict[str, Any]] = []
    for p in range(first, last + 1):
        img, _ = _render_page_to_cgimage(doc, p, 200.0)
        text = _vision_ocr(img, langs)
        if not text:
            continue
        flat = " ".join(text.split())
        idx = flat.lower().find(q)
        while idx != -1 and len(hits) < 10:
            start = max(0, idx - 150)
            hits.append({"page": p, "excerpt": flat[start: idx + len(q) + 250]})
            idx = flat.lower().find(q, idx + len(q))
    if not hits:
        return (f"No exact match for '{query}' in pages {first}-{last}/{n} "
                "of this PDF (not yet ingested, so only the literal text is "
                "matched). Retry with a short keyword in the document's "
                "language, or ingest_document the PDF first and then use "
                "search_notes for meaning-based / cross-language search.")
    return json.dumps({"query": query, "matches": len(hits), "results": hits},
                      ensure_ascii=False, indent=1)


@_extra_tool
def export_page_images(
    path: str,
    output_dir: str,
    first_page: int = 1,
    last_page: int | None = None,
    image_format: str = "jpeg",
) -> str:
    """Export PDF pages as JPG/PNG images - e.g. to drag into the Jan chat for a
    vision model. image_format: "jpeg" or "png". Returns the files written."""
    dpi = 150.0
    doc, n, resolved = _load_pdf(path)
    first, last = _norm_range(first_page, last_page, n)
    out_dir = os.path.abspath(os.path.expanduser(output_dir))
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(resolved))[0]
    ext = ".jpg" if image_format.lower().startswith("j") else ".png"
    files: list[str] = []
    for p in range(first, last + 1):
        img, _ = _render_page_to_cgimage(doc, p, dpi)
        files.append(_write_cgimage(img, os.path.join(out_dir, f"{stem}_p{p:04d}{ext}")))
    return json.dumps({"written": len(files), "files": files}, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# Self-test (khong can MCP):  python server.py --selftest file.pdf
# ---------------------------------------------------------------------------

def _selftest(path: str, dpi: float, langs: str) -> int:
    doc, n, resolved = _load_pdf(path)
    lang_list = [s.strip() for s in langs.split(",")]
    print(f"[selftest] {resolved} - {n} trang, text layer: {sorted(_has_text_layer(resolved))}",
          file=sys.stderr)
    texts: dict[int, str] = {}
    for p in range(1, n + 1):
        img, (w, h) = _render_page_to_cgimage(doc, p, dpi)
        text = _vision_ocr(img, lang_list)
        texts[p] = text
        print(f"[selftest] trang {p}: {w}x{h}px -> OCR {len(text)} ky tu", file=sys.stderr)
        print(f"--- Trang {p} ---")
        print(text[:2000])
    side = os.path.splitext(resolved)[0] + "_ocr.md"
    with open(side, "w", encoding="utf-8") as f:
        f.write("\n\n".join(f"## Trang {p}\n{t}" for p, t in texts.items()))
    print(f"[selftest] da ghi: {side}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", metavar="PDF", help="OCR thu 1 file PDF roi in ket qua")
    ap.add_argument("--dpi", type=float, default=200.0)
    ap.add_argument("--langs", default=DEFAULT_LANGS)
    args, _ = ap.parse_known_args()
    if args.selftest:
        return _selftest(args.selftest, args.dpi, args.langs)
    try:  # prewarm bge-m3 để search_notes đầu tiên không dính cold start
        server_extra._ensure_embed_server(wait=False)
    except Exception:
        pass
    MCP.run(transport="stdio")
    return 0



# ---------------------------------------------------------------------------
# Mo rong: DOCX / MD / anh + ingest vao Vault Obsidian
# ---------------------------------------------------------------------------

import server_extra  # noqa: E402
_mod = sys.modules.get(__name__)
if _mod is None:  # khi duoc import nhu module thuong
    import types as _types
    _mod = _types.SimpleNamespace(**{k: v for k, v in globals().items()
                                     if not k.startswith('__')})
server_extra.S = _mod


@_extra_tool
def ocr_image(path: str, languages: str = DEFAULT_LANGS) -> str:
    """OCR a single image file (png/jpg/heic/webp/tiff/bmp) with the Mac's local
    AI OCR. Returns the recognized text."""
    langs = [s.strip() for s in languages.split(",") if s.strip()]
    return server_extra.ocr_image_file(path, langs) or "*(khong nhan dien duoc chu)*"


@_extra_tool
def ocr_docx(path: str, languages: str = DEFAULT_LANGS, copy_images: bool = True) -> str:
    """Read a Word .docx: text + tables + OCR of every embedded image ->
    markdown. With copy_images=true the images are saved into
    Vault/Attachments/<file-name>/ so they preview in Obsidian."""
    langs = [s.strip() for s in languages.split(",") if s.strip()]
    vault = server_extra.VAULT_DEFAULT
    stem = os.path.splitext(os.path.basename(os.path.expanduser(path)))[0]
    attach = os.path.join(vault, "Attachments", stem) if copy_images else None
    md, n_img, _ = server_extra._docx_to_md(path, langs, attach, None)
    return (f"# {os.path.basename(path)} (anh nhung da OCR: {n_img})\n\n" + md)


@MCP.tool()
def ingest_document(path: str, dest: str = "Inbox",
                    languages: str = DEFAULT_LANGS) -> str:
    """Pipeline: take any file (.pdf | .docx | .md | image) -> local OCR ->
    write a .md note into the Vault (default ~/Documents/Vault/Inbox); images
    are copied to Vault/Attachments/<file-name>/. The note gets frontmatter
    (source, OCR date) for provenance. dest: "Inbox" (default) or "Notes".
    Returns a JSON summary."""
    return server_extra.ingest_document(path, None, dest, languages, 200.0,
                                        True)


@_extra_tool
def batch_ingest(folder: str, dest: str = "Inbox",
                 languages: str = DEFAULT_LANGS,
                 skip_existing: bool = True) -> str:
    """Ingest a whole FOLDER at once: every supported file (.pdf .docx .md /
    images) is OCR'd and written as a .md note into the Vault. Files whose note
    already exists (and is newer) are skipped when skip_existing=true. Use this
    to prepare a batch of documents for RAG indexing in one shot."""
    return server_extra.batch_ingest(folder, None, dest, languages, 200.0,
                                     skip_existing)


@MCP.tool()
def list_notes() -> str:
    """List every document already in the Vault (file name, source file,
    page count, date ingested). Call this when the user asks what documents
    exist, or refers to a document by a vague name - then use the exact
    `file` value with read_note / search_notes."""
    return server_extra.list_notes()


@MCP.tool()
def read_note(file: str, first_page: int = 1, last_page: int | None = None) -> str:
    """Read the FULL text of pages first_page..last_page of one Vault
    document (instant, no OCR). Use it after search_notes to read the whole
    page around a hit (e.g. page-1..page+1), or to summarise a section.
    `file` = the file name from list_notes or a search result (a source
    name like "report.pdf" also works). Read at most ~5 pages per call."""
    return server_extra.read_note(file, first_page, last_page)


@MCP.tool()
def search_notes(query: str, max_hits: int = 6) -> str:
    """Search the Vault's already-OCR'd notes (.md in Inbox/ and Notes/) -
    instant, zero OCR cost. Use this first when the user refers to a document
    that was ingested into the Vault or uploaded to a Project. Accepts FULL
    QUESTIONS - you do NOT need to shorten or rephrase:
      1) tries the whole query as an exact phrase first (mode
         "exact-phrase"); if it matches, that is returned directly;
      2) otherwise runs a SEMANTIC cross-language search (local bge-m3
         embeddings) - a VIETNAMESE question WILL match ENGLISH notes by
         meaning, no translation needed - together with keyword
         page-ranking. Response "mode" is one of:
           "semantic"        strong semantic matches;
           "semantic-low"    weak semantic matches, keyword hits weaker;
           "keywords-ranked" keyword hits (may carry "semantic_low_results");
           "keywords+semantic" with matches 0 - nothing found.
    Each result carries "file" and "page" - always report the page number
    to the user when quoting.
    Only state 'content is absent' when mode is "keywords+semantic" with
    zero matches AND the response has no "semantic_error" field.
    Excerpts are COMPLETE paragraphs - quote verbatim ONLY from the excerpt
    text; never complete a truncated sentence from imagination. If you need
    more surrounding text, call again with a phrase copied from the excerpt.
    Ask in any language - do NOT translate the question yourself."""
    return server_extra.search_notes(query, None, max_hits)


if __name__ == "__main__":
    sys.exit(main())
