# jan-vision-docs-mcp

Local MCP server đọc PDF (kể cả bản scan / ảnh chụp) bằng **AI OCR chạy 100% local
trên Mac** — Apple Vision framework (Live Text), không gửi dữ liệu đi đâu, hỗ trợ
tiếng Việt (`vi-VT`) + tiếng Anh.

Dùng cho **Jan** (https://jan.ai) qua MCP, hoạt động với cả model local lẫn cloud.

## Cấu trúc

```
jan-vision-docs-mcp/
├── server.py           # MCP server (stdio) + 4 tools
├── make_test_pdf.py    # sinh PDF test (text vector + trang giả lập scan)
├── requirements.txt
└── .venv/              # môi trường (đã cài sẵn)
```

## Tools

| Tool | Mục đích |
|---|---|
| `pdf_info` | Số trang, kích thước, trang nào có text layer sẵn — gọi trước khi OCR |
| `ocr_pdf` | OCR khoảng trang → markdown `## Trang N`; `output_md_path` để ghi file `.md` cho PDF dài; `include_images=true` kèm ảnh JPEG của trang (model vision) |
| `search_pdf` | OCR rồi tìm từ khoá, trả excerpt theo trang — tiết kiệm context với PDF dài |
| `export_page_images` | Xuất trang PDF ra JPG/PNG để kéo-thả vào chat Jan |

## Test không cần Jan

```bash
cd ~/Documents/AI/jan-vision-docs-mcp
.venv/bin/python server.py --selftest /tmp/pdfocr_test/test.pdf
# trong Jan: thử prompt "Đọc /tmp/pdfocr_test/test.pdf và tóm tắt điều 5"
```

## Cấu hình vào Jan (đã làm sẵn)

`~/Library/Application Support/Jan/data/mcp_config.json`:

```json
"vision-docs": {
  "command": "~/Documents/AI/jan-vision-docs-mcp/.venv/bin/python",
  "args": ["~/Documents/AI/jan-vision-docs-mcp/server.py"],
  "env": { "PDF_OCR_LANGS": "vi-VT,en-US" },
  "active": true
}
```

`mcpSettings.toolCallTimeoutSeconds` đã tăng 30 → 180 (OCR nhiều trang lâu hơn).
Restart Jan → Settings → MCP Servers phải thấy `vision-docs` active.

## Lưu ý khi dùng trong Jan

1. **Model phải hỗ trợ tool calling** — model local bật "tools" trong Model
   Capabilities; anthropic/gpt thì chọn model có nhãn `tools`.
2. `include_images=true` chỉ dùng với **model vision** (Jan sẽ báo như Browser MCP).
3. Không OCR được PDF có mật khẩu (server sẽ báo lỗi rõ ràng).
4. DPI mặc định 200 làm chữ ~2.8x to hơn font gốc — bản scan mờ/lệch tăng lên 300.

## Kỹ thuật

- Render trang bằng **Quartz (CGPDFDocument)** → CGImage
- OCR bằng **VNRecognizeTextRequest** (Vision), level `Accurate`, language-correction on
- Sắp lại thứ tự dòng theoreading order (Vision trả toạ độ gốc dưới-trái)
- Xuất ảnh bằng CGImageDestination (JPEG/PNG), trả base64 `ImageContent` theo chuẩn MCP

---

## Bản mở rộng: Vault Obsidian + DOCX/MD/ảnh (v2)

```bash
~/Documents/Vault/          # vault mặc định (đặt lại qua env PDF_OCR_VAULT)
├── Inbox/                   # note OCR thô, chờ duyệt — KHÔNG đưa vào RAG index
├── Notes/                   # đã dọn — đưa file này vào Jan Project để index
└── Attachments/<tên-file>/  # ảnh được copy vào (link Obsidian tương đối)
```

### Tools mới

| Tool | Mục đích |
|---|---|
| `ocr_image` | OCR ảnh rời (png/jpg/heic/webp/tiff/bmp) |
| `ocr_docx` | Đọc .docx: văn bản + bảng + OCR từng ảnh nhúng → markdown |
| `ingest_document` | **Pipeline một-lệnh**: nhận `.pdf`/`.docx`/`.md`/ảnh → OCR → ghi note `.md` vào `Vault/Inbox` (frontmatter: nguồn, ngày OCR), copy ảnh vào `Attachments/` |

### Note sinh ra có gì

- Frontmatter: `source`, `source_path`, `ocr_langs`, `ocr_date` — truy vết file gốc
- Ảnh: link Obsidian tương đối `../Attachments/...` (preview được ngay) + khối `> **OCR ảnh:**` chứa text nhận diện để RAG index được nội dung ảnh
- `.md` nguồn: ảnh tham chiếu (cả syntax `![](...)` lẫn `![[...]]`) được OCR và copy ảnh vào vault

### Quy trình khuyến nghị

1. `ingest_document` (hoặc nhờ Jan gọi) → note vào `Inbox/`
2. Đọc lại, chỉnh `source_path`/tag/backlink → chuyển sang `Notes/`
3. Upload các file trong `Notes/` vào Jan **Project** để RAG index (tăng `retrieval_limit` lên ~8-10)

### Test

`.venv/bin/python server.py --selftest <file.pdf>` — selftest chỉ áp dụng PDF;
DOCX/MD/ảnh: `ingest_document(path)` qua MCP hoặc gọi trực tiếp.

---

## v3: Tự resolve tên file → đường dẫn (fix "upload rồi mà bảo không thấy")

**Hiện tượng:** attach PDF kiểu *inline* trong Jan chat → model gọi tool với path
bịa (`/Cambridge IELTS 10.pdf`) → không thấy file.

**Nguyên nhân:** attachment *inline* của Jan **không phải file trên đĩa** — Jan parse
text PDF nhét thẳng vào message (metadata `inline_file_contents`), model chỉ nhận
được *tên* file. Với PDF scan thì text Jan parse được còn là rác (watermark).

**Fix trong server:** mọi tool nhận `path` giờ chạy qua `server_extra.resolve_file()`:
1. Đường dẫn đúng → dùng luôn
2. Không tồn tại → dò **tên file** (basename) trong Downloads/Desktop/Documents/
   thư mục data Jan + **Spotlight `mdfind`** toàn máy, lấy bản mới nhất
3. Không thấy → báo lỗi kèm hướng dẫn để model xin path thật

Test: `pdf_info("/Cambridge IELTS 10.pdf")` → tự tìm ra
`~/Downloads/Cambridge IELTS 10.pdf` (178 trang, toàn scan — cần OCR).

**Khuyến nghị khi dùng:** với file PDF scan, khỏi attach inline — gõ thẳng
"OCR ~/Downloads/Cambridge IELTS 10.pdf trang 10-20" hoặc dùng
`search_pdf` cho tìm kiếm.

---

## Quy ước ngôn ngữ (v5)

| Lớp | Ai đọc | Ngôn ngữ |
|---|---|---|
| Tool docstrings, server instructions, error/JSON messages cho model | LLM (Jan) | **English** — model nhỏ parse schema tiếng Anh tốt hơn và tốn ít token hơn |
| README, hướng dẫn sử dụng, log selftest console | Người | **Tiếng Việt có dấu** |
| Nội dung sinh vào note Vault (placeholder `*(trang trống)*`, cảnh báo `⚠️ ảnh không tìm thấy`) | Người đọc note | **Tiếng Việt có dấu** |
| **Cấm** | — | Tiếng Việt **không dấu** — model hiểu tệ hơn cả tiếng Việt có dấu, người đọc thì mệt |


---

## v6: Automation pipeline — watch folder + batch_ingest

KHÔNG cần fork Jan. Thay vào đó giảm ma sát nạp liệu về mức "kéo-thả".

### Watch folder (launchd, chạy nền tự động)

```bash
~/Documents/Vault/_dropbox/     ← THẢ FILE VÀO ĐÂY (pdf/docx/md/ảnh)
                    │  launchd agent quét mỗi 60s (file phải copy xong ≥15s)
                    ▼
        Vision OCR → note .md vào ~/Documents/Vault/Inbox/
        file gốc chuyển sang _dropbox/_done/  (lỗi → _failed/)
        log: watch.log
```

- Cài (đã cài sẵn): `bash install_watch.sh`
- Tắt: `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.jan.visdocs.watch.plist`
- Gỡ hẳn: xóa file plist trên

### Tool `batch_ingest` (gọi từ Jan chat hoặc dùng tay)

`batch_ingest(folder=...)` — OCR cả thư mục một lượt, note `.md` vào `Inbox/`,
file đã có note mới hơn thì skip (`skip_existing=true`). Dùng trước khi
upload một loạt vào Jan Project (Jan không OCR — phải từ .md).

### Luồng chuẩn cho PDF scan (từ 0.8.8)

thả scan vào `_dropbox` (hoặc `batch_ingest`) → duyệt note ở `Inbox/` → chuyển
sang `Notes/` → upload file `.md` vào Jan Project → chunk + embed + KNN chạy tốt.

## v3.1 — hardening search_notes (QA 20/11/2026)

Suite test 12 case đa chủng (cross-lang Việt↔Anh, paraphrase thuần Việt, số/bảng
tài chính, truy vấn rác, cache-rebuild): **12/12 pass**. Các vá:

- **Strip YAML frontmatter** ra khỏi index tìm kiếm (`_read_note`): frontmatter
  không còn là "trang ma" cạnh tranh keyword, excerpt không còn mở đầu bằng
  `--- source: ...`.
- **Tokenizer tier-2 nhận số** (`12,8` / `18%` / `2026`): truy vấn tài chính,
  hóa đơn, báo cáo giờ match được số liệu trong bảng.
- **`_paragraph_around` aware heading**: cắt đoạn tại `\n\n`, `## Trang N`,
  `---` (OCR Vision không có dòng trống trong trang) và center cửa sổ 1500 ký
  tự vào đúng vị trí keyword — 15 hits nước mắt không còn chung 1 excerpt.
- Sửa locale OCR `vi-VT` → `vi-VN`.

Known-limit: câu hỏi "gần nghĩa" ở ngưỡng 0.50–0.53 (vd *giá vé máy bay* match
đoạn travel booking) có thể ra false-positive nhẹ — ngưỡng semantic giữ 0.45 vì
các match chuẩn chạy 0.55–0.85.

## Cài trên máy khác (từ bản zip)

Yêu cầu máy đích: **macOS 13+** (Apple Silicon tốt nhất), `python3`,
`brew install llama.cpp` (cho semantic search — không có thì tier đó tự tắt),
**Jan app**. Sau đó chỉ 1 lệnh:

```bash
unzip jan-vision-docs-mcp-v3.1.zip && cd jan-vision-docs-mcp
bash install.sh
```

`install.sh` tự làm: tạo `.venv` + cài deps → tạo Vault (`~/Documents/Vault`
hoặc `$PDF_OCR_VAULT`) → tìm/thỏ tải **bge-m3-Q8_0.gguf** (~634MB, kiểm SHA256)
về `models/` → đăng ký server `vision-docs` vào `mcp_config.json` của Jan (có backup,
idempotent) → cài launchd watch agent. Cờ: `--no-jan --no-watch --no-model`.

Lưu ý cho máy đích:
- **Restart Jan** sau khi cài để nạp MCP server.
- Model embedding của *Jan project* (RAG file .md upload vào project) mặc định
  là `all-MiniLM-L6-v2` (chỉ tiếng Anh). Muốn tìm tiếng Việt cross-language
  mạnh như trên máy này, thay `model.gguf` của thư mục
  `.../Jan/data/llamacpp/models/sentence-transformer-mini/` bằng file
  `bge-m3-Q8_0.gguf` vừa tải (giữ nguyên `model.yml`), rồi xoá và upload lại
  các file trong project để Jan re-index (chunk vectors 1024 chiều).
- Gỡ: `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.jan.visdocs.watch.plist`,
  xoá entry `vision-docs` trong `mcp_config.json`, xoá thư mục này.

## 🤖 Prompt cho agent tự cài (dán vào terminal agent trên máy mới)

Nếu dùng agent CLI (Claude Code / Codex / Vibe...) trên máy đích, dán block
này vào — agent sẽ chủ động kiểm tra, cài và verify không cần hỏi từng bước:

```text
You are a setup agent on this macOS machine. Install and verify the
jan-vision-docs-mcp local MCP server so Jan can OCR + search documents.

Repo: https://github.com/luongndcoder/jan-vision-docs-mcp

Step 1 — Preconditions (check first, fix autonomously when safe):
- python3 >= 3.9 present? (`python3 --version`). If missing install via brew.
- `command -v llama-server` fails -> `brew install llama.cpp`.
  (Homebrew missing -> install brew first, it is required for llama.cpp.)
- Jan app data dir `~/Library/Application Support/Jan` exists?
  If not, tell the user to install Jan app first, then continue with the
  rest and let the user rerun `install.sh` after Jan is installed.

Step 2 — Get the code:
git clone https://github.com/luongndcoder/jan-vision-docs-mcp.git \
  ~/Documents/AI/jan-vision-docs-mcp
(If a zip archive was provided instead, unzip it to that same folder.)

Step 3 — Install (uses default vault ~/Documents/Vault; prefix
PDF_OCR_VAULT=/path/to/vault to change):
cd ~/Documents/AI/jan-vision-docs-mcp && bash install.sh
Expect: venv created, deps installed, bge-m3-Q8_0.gguf (~634 MB) downloaded
with SHA256 verification, Jan mcp_config.json patched (a .bak backup is
created), launchd watcher installed. If the download fails, retry once.
Do not skip or bypass the SHA256 check.

Step 4 — Verify (all must pass; fix and re-verify if not):
- `.venv/bin/python -c "import server"` runs without error (from repo dir).
- `launchctl list | grep ai.jan.visdocs.watch` shows the watcher.
- Drop ANY test file (pdf/docx/md/png) into <vault>/_dropbox/, wait up to
  90 s, confirm a new .md note appears in <vault>/Inbox/.
- In the repo dir, run:
  .venv/bin/python -c "import json,server;r=json.loads(\
  server.server_extra.search_notes('test'));print(r['mode'],r['matches'])"
  It must not crash (0 matches is fine for a fresh vault).

Step 5 — Finish: tell the user to FULLY QUIT and restart Jan (MCP servers
load at startup), then verify in Jan. Report back: vault path, what was
installed, whether Jan restart is still pending, and the uninstall
commands. Never claim verification you did not actually perform.
```

> Ghi chú: các biến môi trường giữ tiền tố `PDF_OCR_*` (`PDF_OCR_VAULT`,
> `PDF_OCR_LANGS`, `PDF_OCR_EMB_MODEL`...) để tương thích ngược — đổi tên
> repo/server không ảnh hưởng chúng.
