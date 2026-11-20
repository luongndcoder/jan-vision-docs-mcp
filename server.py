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
        "Read PDF/DOCX/MD/TXT/images locally on the Mac using Apple's Vision OCR. "
        "CRITICAL RULE - NEVER INVENT A FILENAME: only search files that the "
        "user actually mentioned, that exist in the conversation, or that are "
        "in ~/Documents/Vault (Inbox/ or Notes/). If the user's question does "
        "not name a file, do NOT guess one - the answer is usually in the "
        "Vault notes: call search_notes with keywords, or ask the user WHICH "
        "document. USAGE: (1) attachments in chat are NOT files on disk - "
        "pass the name and the server will locate it; (2) to FIND WHERE "
        "content appears in a document use search_notes (instant, searches "
        "all Vault notes) or search_pdf with keywords IN THE DOCUMENT'S "
        "LANGUAGE (English docs need English keywords - translate the user's "
        "Vietnamese query first); (3) PDFs over 25 pages: ocr_pdf writes a "
        "full .md and returns a preview - read that file, do not re-OCR; (4) "
        "to add a new document to the vault use ingest_document or "
        "batch_ingest; (5) call pdf_info before OCR-ing a PDF."
    ),
)

MAX_INLINE_TEXT_CHARS = 30_000  # qua day se cat va ghi ban day du ra file .md


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
    reader = pypdf.PdfReader(path)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            return {}
    has: dict[int, bool] = {}
    for i, page in enumerate(reader.pages[:max_pages], start=1):
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

@MCP.tool()
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


@MCP.tool()
def ocr_pdf(
    path: str,
    first_page: int = 1,
    last_page: int | None = None,
    dpi: float = 200.0,
    languages: str = DEFAULT_LANGS,
    output_md_path: str | None = None,
    include_images: bool = False,
    max_images: int = 5,
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
    Set include_images=true only for vision-capable models (keep max_images small).
    """
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
    dpi: float = 200.0,
    languages: str = DEFAULT_LANGS,
    context_chars: int = 260,
) -> str:
    """OCR locally (Vision) then find `query` - returns PAGE + surrounding text.
    THIS is the tool for 'where is X mentioned in the PDF'; far cheaper than
    ocr_pdf.
    NOTE: query must be a keyword in the DOCUMENT'S language (an English PDF
    needs English keywords such as hunt/trade/pilgrim/migration - not a
    Vietnamese translation). Call once per keyword."""
    doc, n, resolved = _load_pdf(path)
    first, last = _norm_range(first_page, last_page, n)
    langs = [s.strip() for s in languages.split(",") if s.strip()] or DEFAULT_LANGS.split(",")
    q = query.strip().lower()
    if not q:
        return "Empty query."

    hits: list[dict[str, Any]] = []
    for p in range(first, last + 1):
        img, _ = _render_page_to_cgimage(doc, p, dpi)
        text = _vision_ocr(img, langs)
        if not text:
            continue
        flat = " ".join(text.split())
        idx = flat.lower().find(q)
        while idx != -1 and len(hits) < 40:
            start = max(0, idx - context_chars // 2)
            hits.append({"page": p, "excerpt": flat[start: idx + context_chars]})
            idx = flat.lower().find(q, idx + len(q))
    if not hits:
        return f"No match for '{query}' in pages {first}-{last}/{n}."
    return json.dumps({"query": query, "matches": len(hits), "results": hits},
                      ensure_ascii=False, indent=1)


@MCP.tool()
def export_page_images(
    path: str,
    output_dir: str,
    first_page: int = 1,
    last_page: int | None = None,
    dpi: float = 150.0,
    image_format: str = "jpeg",
) -> str:
    """Export PDF pages as JPG/PNG images - e.g. to drag into the Jan chat for a
    vision model. Returns the list of files written."""
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


@MCP.tool()
def ocr_image(path: str, languages: str = DEFAULT_LANGS) -> str:
    """OCR a single image file (png/jpg/heic/webp/tiff/bmp) with the Mac's local
    AI OCR. Returns the recognized text."""
    langs = [s.strip() for s in languages.split(",") if s.strip()]
    return server_extra.ocr_image_file(path, langs) or "*(khong nhan dien duoc chu)*"


@MCP.tool()
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
def ingest_document(path: str, vault_root: str = server_extra.VAULT_DEFAULT,
                    dest: str = "Inbox", languages: str = DEFAULT_LANGS,
                    dpi: float = 200.0, copy_images: bool = True) -> str:
    """Pipeline: take any file (.pdf | .docx | .md | image) -> local OCR ->
    write a .md note into the Vault (default ~/Documents/Vault/Inbox); images
    are copied to Vault/Attachments/<file-name>/. The note gets frontmatter
    (source, OCR date) for provenance. Returns a JSON summary."""
    return server_extra.ingest_document(path, vault_root, dest, languages, dpi,
                                        copy_images)


@MCP.tool()
def batch_ingest(folder: str, vault_root: str = server_extra.VAULT_DEFAULT,
                 dest: str = "Inbox", languages: str = DEFAULT_LANGS,
                 dpi: float = 200.0, skip_existing: bool = True) -> str:
    """Ingest a whole FOLDER at once: every supported file (.pdf .docx .md /
    images) is OCR'd and written as a .md note into the Vault. Files whose note
    already exists (and is newer) are skipped when skip_existing=true. Use this
    to prepare a batch of documents for RAG indexing in one shot."""
    return server_extra.batch_ingest(folder, vault_root, dest, languages, dpi,
                                     skip_existing)


@MCP.tool()
def search_notes(query: str, max_hits: int = 15) -> str:
    """Search the Vault's already-OCR'd notes (.md in Inbox/ and Notes/) -
    instant, zero OCR cost. Use this first when the user refers to a document
    that was ingested into the Vault or uploaded to a Project. Accepts FULL
    QUESTIONS - you do NOT need to shorten or rephrase:
      1) tries the whole query as an exact phrase first;
      2) if no phrase match, automatically ranks best PAGES by keyword overlap;
      3) if keywords also find nothing, runs a SEMANTIC cross-language search
         (local bge-m3 embeddings) - so a VIETNAMESE question WILL match
         ENGLISH notes by meaning. This takes care of translation itself.
    Only state 'content is absent' when the final semantic mode (visible in
    the response "mode" field) returns zero matches.
    Excerpts are COMPLETE paragraphs - quote verbatim ONLY from the excerpt
    text; never complete a truncated sentence from imagination. If you need
    more surrounding text, call again with a phrase copied from the excerpt.
    For Vietnamese queries over English documents, include or translate to
    the document's language keywords (e.g. travel, trade, war, migration)."""
    return server_extra.search_notes(query, None, max_hits)


if __name__ == "__main__":
    sys.exit(main())
