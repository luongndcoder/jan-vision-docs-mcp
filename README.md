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
  "command": "<vị-trí-repo>/.venv/bin/python",
  "args": ["<vị-trí-repo>/server.py"],
  "env": { "PDF_OCR_LANGS": "vi-VN,en-US", "PDF_OCR_VAULT": "<vault>" },
  "active": true
}
```

> `<vị-trí-repo>` là đường dẫn tuyệt đối tới thư mục gốc trên máy — `install.sh`
> (bước patch_jan_config.py) tự điền đúng, không phải copy tay.

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
├── Inbox/                   # note OCR mới — được auto-sync vào Jan project
├── Notes/                   # tuỳ chọn: chỗ sắp xếp note đã dọn (cũng được sync)
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

1. Kéo thả file vào web UI (hoặc `ingest_document`) → note vào `Inbox/`
2. Note tự sync vào Jan project đã chọn — cả `Inbox/` lẫn `Notes/` đều vào RAG,
   không có bước duyệt bắt buộc. Muốn sửa lỗi OCR thì sửa note rồi bấm
   **Đồng bộ tất cả** (re-index theo nội dung, không theo kích thước file).
3. `Notes/` chỉ để sắp xếp; chuyển note qua lại rồi bấm **Đồng bộ tất cả** để
   cập nhật path. Tăng `retrieval_limit` của project lên ~8-10.

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

### Watch folder (TÙY CHỌN — mặc định TẮT từ v4)

Luồng chuẩn giờ là **web upload**. Watcher chỉ bật khi cài với
`bash install.sh --with-watch`, hoặc chạy riêng `bash install_watch.sh`:

```bash
~/Documents/Vault/_dropbox/     ← (khi bật) thả file vào đây
                    │  launchd agent quét mỗi 60s (file cũ ≥15s)
                    ▼
        Vision OCR → note .md vào ~/Documents/Vault/Inbox/
        file gốc chuyển sang _dropbox/_done/  (lỗi → _failed/)
        log: watch.log
```

- Tắt: `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.jan.visdocs.watch.plist`
- Gỡ hẳn: xóa file plist trên + thư mục `_dropbox`

### Tool `batch_ingest` (gọi từ Jan chat hoặc dùng tay)

`batch_ingest(folder=...)` — OCR cả thư mục một lượt, note `.md` vào `Inbox/`,
file đã có note mới hơn thì skip (`skip_existing=true`). Dùng trước khi
upload một loạt vào Jan Project (Jan không OCR — phải từ .md).

### Luồng chuẩn (từ v4 — chỉ web upload)

mở **http://127.0.0.1:8765** → kéo thả file vào trang → note `.md` vào `Inbox/`,
gốc vào `_done_web/` → chọn Jan project trên web → tự sync khi OCR xong (hoặc
bấm "Đồng bộ tất cả") → mở Jan hỏi. Không cần Finder, không cần kéo file vào
project nào cả.

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
  `bge-m3-Q8_0.gguf` vừa tải, và trong `model.yml` đổi `pooling: mean` →
  `pooling: cls`; restart Jan rồi bấm **Đồng bộ tất cả** (chunk vectors 1024
  chiều). **Bắt buộc** nếu dùng sync từ web UI: web UI kiểm tra model này
  (`/jan-embed`, CLI `jan_sync.py --embed-check`) và chặn sync + hiện cảnh
  báo khi Jan còn dùng MiniLM 384 chiều — nếu không, RAG trong Jan tìm không
  ra gì mà không báo lỗi.
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
- `launchctl list | grep ai.jan.visdocs` shows `watch` and `webui` agents.
- `curl -s http://127.0.0.1:8765/healthz` returns "ok" (web upload UI).
- Open http://127.0.0.1:8765 in a browser and drop ANY test file
  (pdf/docx/md/txt/png) onto the page; wait up to 90 s; confirm the job card
  turns "done" and the note appears in the vault list (<vault>/Inbox/).
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

## 🌐 Web UI upload (chỉ localhost)

Cho người dùng không rành Finder/vault: mở **http://127.0.0.1:8765** — trang web
kéo-thả file (PDF/DOCX/MD/TXT/ảnh), hiện tiến trình OCR từng file thành công/lỗi
theo thời gian thực, liệt kê note trong vault, xem trước và tải file `.md`.

```
webui.py            ← FastAPI, bind 127.0.0.1 (máy khác KHÔNG truy cập được)
install_webui.sh    ← launchd ai.jan.visdocs.webui (KeepAlive, chạy nền)
POST /upload        ← xếp hàng, 1 worker OCR tuần tự (Vision + embedding)
GET  /jobs          ← trạng thái từng file: pending → running → done/failed
                    ← file gốc sau khi OCR chuyển vào Vault/_done_web/
```

- Vault/port: env `PDF_OCR_VAULT`, `PDF_OCR_WEBUI_PORT` (mặc định 8765).
- Không đụng `_dropbox/` — kể cả khi bật watcher tùy chọn, hai luồng không
  xử lý trùng file.
- Cài: `bash install_webui.sh` (install.sh tự gọi). Gỡ: `launchctl bootout
  gui/$(id -u) ~/Library/LaunchAgents/ai.jan.visdocs.webui.plist`.

## 🔁 Đồng bộ thẳng vào Jan project (không cần mở Jan kéo file)

Bước thủ công "mở Jan → kéo .md vào project" giờ không cần nữa. Jan index
file theo **đường dẫn gốc** trong `data/db/project_<id>.db`:

```
files(id, path UNIQUE, name, type, size, chunk_count)
chunks(id, text, embedding BLOB float32-1024, file_id, chunk_file_order)
```

`jan_sync.py` tái hiện đúng pipeline của Jan: cắt chunk **512 ký tự** (không
overlap — check với project 637 chunks do chính Jan tạo), embedding **bge-m3**
qua llama-server local, ghi float32 little-endian cùng format. Note được
thêm/đổi **nội dung** thì re-index (DELETE + INSERT transaction); note trong
vault đã xoá/di chuyển bị dọn khỏi index khi bấm **Đồng bộ tất cả**
(file Jan tự index ngoài vault không bị đụng tới).

- **Tự động**: OCR xong là note vào project đang chọn + toast thông báo.
- Trong tab **Thư viện**: chọn Jan project (select) → từng note có nút
  `→ Jan` / `✓ Jan nk`, hoặc bấm **Đồng bộ tất cả**.
- CLI: `.venv/bin/python jan_sync.py --list | --all | --embed-check | "<note.md>"`

Yêu cầu: đã có project trong Jan (tạo 1 lần trong app) và model embedding
của Jan là bge-m3 (xem mục cài đặt). Di chuyển/xoá note trong Obsidian →
bấm **Đồng bộ tất cả** để cập nhật path và dọn entry cũ.

**Hai nguồn tra cứu trong Jan:** RAG của project (Jan tự chèn chunk vào
hội thoại — hỏi đáp chung, tóm tắt) và tool `search_notes` (trích dẫn có số
trang, kiểm số liệu, xác nhận "không có trong tài liệu"). Server instructions
hướng model dùng đúng cái; chunk RAG không có số trang.

## 🌏 Chọn ngôn ngữ OCR (từng lần nạp)

Dưới khung thả file có **chips ngôn ngữ**: Việt · Anh · Trung giản · Trung
phồn · Nhật · Hàn · Pháp · Đức · Nga — bấm chọn nhiều cái, lựa chọn được
nhớ theo máy (localStorage). Ví dụ sách Trung: bật *Trung giản* (tắt bớt
vi/en nếu muốn nhanh).

- Mọi upload đi kèm `langs` → job dùng đúng bộ ngôn ngữ đó, frontmatter ghi
  `ocr_langs` tương ứng; card hàng đợi hiển thị languages đang dùng.
- Watcher (tùy chọn) dùng `PDF_OCR_LANGS` mặc định — chọn ngôn ngữ từng file
  thì dùng web upload (chips ngôn ngữ ngay trên trang).
- Vision Live Text hỗ trợ đầy đủ Hán (zh-Hans/zh-Hant) — đã test render
  PingFang sạch: OCR đúng 100% ký tự.
- Tìm kiếm sau đó vẫn chạy cross-language vì bge-m3 đa ngữ (tier-2 keyword
  yếu với từ 1-2 ký tự Hán, tier semantic & exact-phrase là chính).

## 📂 Nhiều project: multi-select + custom dropdown

Nhấp chữ **"Chọn project…"** (đã thay `<select>` native — popup macOS xanh
đỏTheme mắt) mở popover dark: mỗi project một hàng (tên · số file · số
chunks) với checkbox, **chọn nhiều đích sync cùng lúc**. Lưu ý:

- Badge mỗi note: `✓ Jan` (đủ mọi project đang chọn) · `◐ 1/2` (một phần)
  · `→ Jan` (chưa có ở đâu) — bấm là sync vào tất cả project đã chọn.
- "Đồng bộ tất cả" và auto-sync sau OCR áp dụng cho mọi project được chọn.
- `/jan-sync` nhận `projects: [id...]`; id không hợp lệ bị bỏ qua.
- Lựa chọn lưu `localStorage` (`vd.projs`, migrate tự động từ `vd.proj` cũ).

## 🎨 Theme sáng/tối + icon cho nút hành động

- Toggle hai theme ở cuối sidebar (sun/moon); **mặc định nền sáng**; lựa
  chọn lưu `localStorage` và áp trước khi trang vẽ (không flash).
- Light: nền ấm `#f7f6f3`, card trắng, accent cam đậm hơn cho tương phản;
  grain noise tự invert, bóng đổ nhẹ hơn. Dark giữ nguyên như cũ.
- Nút "Xem" (con mắt), "Tải .md" (mũi tên xuống), Jan badge (khay vault)
  giờ có icon SVG đồng bộ nét 2px với sidebar.
- Tất cả màu hardcode đã biến hoá thành token (`--line-strong`...), hai
  theme dùng chung 1 bộ biến.

## 👻 Project đã xoá trong Jan vẫn nằm lại? (leak file + cơ chế dọn)

Jan xoá project nhưng **không xoá file `db/project_<id>.db`** trên đĩa (bug/thói
quen của Jan). Server này giờ phát hiện và xử lý:

- `list_projects()` đánh dấu `alive` theo số thread còn tham chiếu project
  (thread metadata) — không có thread nào = đã bị xoá trong Jan.
- Dropdown web chia 2 nhóm: project sống (chọn được) và **"Đã xoá trong Jan
  (file thừa)"** — gạch ngang, không chọn được, kèm nút `Xoá file`.
- Xoá = **chuyển vào `~/.Trash`** (không rm cứng, hoàn tác được trong Finder),
  cần bấm 2 lần (nút đổi thành "Chắc chắn?" trước khi thực hiện). Server từ
  chối xoá nếu project còn `alive`.
