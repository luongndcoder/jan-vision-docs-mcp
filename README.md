# jan-vision-docs-mcp

Stack nghiên cứu tài liệu chạy offline hoàn toàn cho [Jan](https://jan.ai) trên macOS.
OCR bằng Apple Vision, lưu thành note `.md` trong một Vault, tìm kiếm semantic bằng
bge-m3 qua `llama-server` local, có web UI để upload và tự sync note vào Jan project.

## Kiến trúc

```
Web UI (127.0.0.1:8765)
   │ upload PDF/DOCX/MD/TXT/ảnh + chọn ngôn ngữ OCR
   ▼
Apple Vision OCR  ──►  Vault/Inbox/<tên>.md   (file gốc → Vault/_done_web/)
                          │
          ┌───────────────┴────────────────┐
          ▼                                ▼
jan_sync.py: chunk 512 ký tự +       MCP server `vision-docs` (stdio)
bge-m3 embedding, ghi thẳng vào      list_notes / search_notes / read_note
data/db/project_<id>.db của Jan      search_pdf / ingest_document
          │                                │
          └───────────► Jan ◄──────────────┘
                 RAG của project + tool call
```

Jan có hai nguồn tra cứu: RAG của project (chunk tự chèn vào hội thoại, không có số trang)
và tool `search_notes` (có số trang, dùng để trích dẫn và kiểm số liệu).

## Cài đặt

Yêu cầu: macOS 13+, `python3`, Jan app, và `brew install llama.cpp` (không có thì
semantic search tắt, các phần khác vẫn chạy).

```bash
bash install.sh
```

`install.sh` làm lần lượt:

1. Tạo Vault (`~/Documents/Vault`, đổi bằng env `PDF_OCR_VAULT`) với `Inbox/`, `Notes/`, `Attachments/`, `_dropbox/`.
2. Tạo `.venv`, cài dependencies (dùng `wheels/` nếu có, xem mục Chạy offline).
3. Tìm hoặc tải `bge-m3-Q8_0.gguf` (~634MB, kiểm SHA256) vào `models/`.
4. Ghi server `vision-docs` vào `mcp_config.json` của Jan (có backup, chạy lại không sao) và đặt `toolCallTimeoutSeconds` 180 nếu tạo mới config.
5. Cài web UI thành launchd agent `ai.jan.visdocs.webui` (chạy nền, `KeepAlive`).

Flag: `--no-jan` (không patch Jan), `--no-model` (không tải model), `--no-web` (không cài web UI),
`--with-watch` (cài thêm watcher thả file vào `_dropbox/`, mặc định tắt).

Sau đó **restart Jan** để nạp MCP server.

### Bắt buộc: đổi embedding model của Jan sang bge-m3

Model mặc định của Jan project là MiniLM 384 chiều (chỉ tiếng Anh). Sync bằng bge-m3
(1024 chiều) vào project dùng MiniLM sẽ khiến RAG không tìm ra gì mà không báo lỗi.

1. Thay `model.gguf` trong `~/Library/Application Support/Jan/data/llamacpp/models/sentence-transformer-mini/` bằng `models/bge-m3-Q8_0.gguf` (đổi tên thành `model.gguf`).
2. Trong `model.yml` cùng thư mục, đổi `pooling: mean` thành `pooling: cls`.
3. Restart Jan, rồi bấm **Đồng bộ tất cả** trên web UI.

Web UI kiểm tra model này và chặn sync kèm cảnh báo nếu chưa đổi. Kiểm tra bằng CLI:

```bash
.venv/bin/python jan_sync.py --embed-check
```

### Dán prompt cho assistant

Mở Jan → Assistants → Jan → Instructions, dán khối trong
[prompts/jan-assistant-instructions.md](prompts/jan-assistant-instructions.md), lưu rồi tạo
**thread mới** (thread cũ giữ prompt cũ). Model phải hỗ trợ tool calling.

## Cài bằng agent (máy mới)

Máy mới có agent CLI (Claude Code, Codex…): mở terminal, chạy agent rồi dán nguyên khối dưới đây.
Agent tự kiểm tra, cài, thay embedding model của Jan và verify; chỉ dừng lại hỏi khi cần người dùng
thao tác (cài/thoát Jan, dán prompt). Prompt viết tiếng Anh cho agent đọc chuẩn hơn.

````text
You are a setup agent on this macOS machine. Install and verify jan-vision-docs-mcp:
a fully OFFLINE document-research stack for the Jan desktop app (Apple Vision OCR,
a Vault of .md notes, bge-m3 semantic search via a local llama-server, a localhost
web UI, and an MCP server for Jan). Work autonomously; stop and ask the user only
where a step says ASK USER.

Ground rules
- Never claim a check passed unless you ran it and saw the output. Report failures verbatim.
- Before modifying any file under ~/Library/Application Support/Jan, make a .bak copy.
- Never skip or bypass the SHA256 check of the model download.
- Do not modify Jan files while Jan is running (check: `pgrep -f "Jan.app"` must print nothing).

Step 1 - Preconditions (fix what you safely can)
- macOS 13+ (`sw_vers`); Apple Silicon preferred (`uname -m` = arm64).
- python3 >= 3.9 (`python3 --version`); if missing: `xcode-select --install` or `brew install python`.
- Homebrew + llama.cpp: `command -v llama-server || brew install llama.cpp`
  (no internet: llama-server must already be installed or copied into /opt/homebrew/bin).
- Jan app: `/Applications/Jan.app` and `~/Library/Application Support/Jan/data` must exist.
  If not: ASK USER to install Jan from https://jan.ai, open it once, then quit it (Cmd+Q).

Step 2 - Get the code
- Online:  git clone https://github.com/luongndcoder/jan-vision-docs-mcp.git ~/Documents/AI/jan-vision-docs-mcp
- Offline: the user copied the repo folder (with `wheels/` and `models/bge-m3-Q8_0.gguf`)
  to ~/Documents/AI/jan-vision-docs-mcp. Use it as is.
- cd into the repo for all remaining steps.

Step 3 - Install
- Run: `bash install.sh`   (vault defaults to ~/Documents/Vault; prefix PDF_OCR_VAULT=/path to change)
  Flags if needed: --no-jan --no-model --no-web --with-watch.
- Expect: .venv created with deps (offline from wheels/ if present), bge-m3-Q8_0.gguf found or
  downloaded (~634 MB, SHA256-checked) into models/, server `vision-docs` registered in
  ~/Library/Application Support/Jan/data/mcp_config.json (a .bak is written), web UI installed as
  launchd agent `ai.jan.visdocs.webui`. If the download fails, retry once.

Step 4 - Switch Jan's embedding model to bge-m3 (MANDATORY)
Jan indexes project files with its own embedder (stock: MiniLM, 384-dim). This stack writes
1024-dim bge-m3 vectors, so Jan's embedder must be bge-m3 or Jan's RAG silently finds nothing.
- ASK USER to quit Jan (Cmd+Q). Wait until `pgrep -f "Jan.app"` prints nothing.
- D="$HOME/Library/Application Support/Jan/data/llamacpp/models/sentence-transformer-mini"
- If "$D/model.gguf" exists and is NOT already ~634 MB: copy model.gguf -> model.gguf.minilm.bak
  and model.yml -> model.yml.minilm.bak, then copy the repo's models/bge-m3-Q8_0.gguf to
  "$D/model.gguf". In "$D/model.yml" set `pooling: cls` and set `size_bytes:` to the new file size.
- If "$D" does not exist (Jan never bootstrapped its embedder, e.g. offline machine): create it,
  copy the model as "$D/model.gguf", and write "$D/model.yml":
      model_path: llamacpp/models/sentence-transformer-mini/model.gguf
      name: all-MiniLM-L6-v2
      size_bytes: <size of model.gguf in bytes>
      embedding: true
      pooling: cls
      ubatch_size: 2048
      batch_size: 2048
- Verify: `.venv/bin/python jan_sync.py --embed-check` must print "ok": true, "dim": 1024, "pooling": "cls".

Step 5 - Verify the stack (all must pass; fix and re-run if not)
- `launchctl list | grep ai.jan.visdocs` shows the webui agent.
- `curl -s http://127.0.0.1:8765/healthz` returns "ok"; `curl -s http://127.0.0.1:8765/jan-embed` has "ok":true.
- MCP tools: `.venv/bin/python -c "import asyncio,server;print([t.name for t in asyncio.run(server.MCP.list_tools())])"`
  must list exactly: search_pdf, ingest_document, list_notes, read_note, search_notes.
- End-to-end: create a small test file, e.g. `printf "## Trang 1\n\nVision docs install test: the quick brown fox jumps over the lazy dog near the river bank today.\n" > /tmp/vd_install_test.md`,
  upload it: `curl -s -F files=@/tmp/vd_install_test.md -F langs=vi-VN,en-US http://127.0.0.1:8765/upload`,
  poll `curl -s http://127.0.0.1:8765/jobs` until its status is "done" (max ~90 s), then run
  `.venv/bin/python -c "import json,server_extra as s;r=json.loads(s.search_notes('con cáo nhảy qua con chó lười'));print(r['mode'],r['matches'])"`
  -> expect matches >= 1 (a Vietnamese query finding English text proves cross-language search).
  Then delete the test note: `curl -s -X POST -H 'Content-Type: application/json' -d '{"name":"vd_install_test.md"}' http://127.0.0.1:8765/note-delete`.
- Offline check (optional, needs Jan open): `bash offline_check.sh 60` -> "External" should list
  at most Jan's own update check (apps.jan.ai).

Step 6 - Hand over to the user (ASK USER to do these in Jan; you cannot click in Jan)
1. Open Jan. Create a Project (e.g. "Docs"); it appears in the web UI project dropdown.
2. Settings -> Assistants -> Jan -> Instructions: paste the block from
   prompts/jan-assistant-instructions.md, save. Always start a NEW thread afterwards.
3. Recommended settings for a 4B local model on Apple Silicon:
   - Model Providers -> Llama.cpp: Flash Attention = on, KV Cache K/V Type = q8_0,
     Prompt Cache RAM = 2048, Cache Reuse = 256, Preserve Reasoning History = off.
   - Model settings (Jan-v3.5-4B): Context Size = 32768, GPU Layers = -2,
     Temperature = 0.6, Top K = 20, Min P = 0.
   - Attachments: Vector Search Mode = Linear, Affinity Threshold = 0.5, Auto inline threshold = 0.4.
   - Disable unused cloud providers so only local models are selectable.
4. Open http://127.0.0.1:8765, select the project, drag-drop documents. Notes auto-sync to Jan.
5. Fully quit Jan (Cmd+Q) and reopen once more so it loads the MCP server and model settings.

Step 7 - Report back
Repo path, vault path, Python version, llama-server path, model path + SHA status, result of every
check in Step 5 (pass/fail with output), whether Step 4 was done, which Step 6 items remain for the
user, and the uninstall commands from the README section "Gỡ cài đặt".
````

## Sử dụng

Mở http://127.0.0.1:8765 (chỉ truy cập được từ máy này; đổi port bằng `PDF_OCR_WEBUI_PORT`).

- **Upload**: kéo thả file, chọn ngôn ngữ OCR bằng chips (Việt, Anh, Trung giản/phồn, Nhật, Hàn, Pháp, Đức, Nga; lựa chọn được nhớ trong trình duyệt). Hàng đợi xử lý tuần tự, hiện tiến trình từng file.
- **Thư viện**: tìm kiếm, lọc, xem trước, tải `.md`. Chọn một hoặc nhiều Jan project làm đích sync.
- **Pill trạng thái Jan** trên mỗi note: `Đưa vào Jan` (chưa có ở đâu), `Jan k/n` (có ở một phần project đã chọn), `Trong Jan` (đủ). Bấm `Đưa vào Jan` để sync. Bấm `Trong Jan` thì pill đổi thành "Gỡ khỏi Jan?"; bấm thêm lần nữa trong 3 giây để gỡ note khỏi các project đã chọn (note vẫn ở Thư viện).
- **Đồng bộ tất cả**: re-index note theo nội dung (không theo kích thước file) và dọn note đã xoá hoặc di chuyển khỏi index. File Jan tự index ngoài Vault không bị đụng tới.
- **Xoá note** (icon thùng rác, bấm 2 lần): chuyển note và ảnh đính kèm vào `~/.Trash` (khôi phục được), gỡ khỏi mọi Jan project.
- OCR xong thì note tự sync vào các project đang chọn.

Cấu trúc Vault:

- `Inbox/`: note OCR mới, tự sync vào Jan.
- `Notes/`: tuỳ chọn, để sắp xếp note; cũng được sync. Di chuyển note rồi bấm **Đồng bộ tất cả** để cập nhật path.
- `Attachments/<tên-file>/`: ảnh của note, link Obsidian tương đối.

CLI tương đương: `.venv/bin/python jan_sync.py --list | --all | --embed-check | "<note.md>"`.
Nên tăng `retrieval_limit` của project lên khoảng 8-10.

## MCP tools

Mặc định server `vision-docs` chỉ lộ 5 tool:

| Tool | Dùng khi |
|---|---|
| `list_notes` | Xem Vault có tài liệu gì (tên, nguồn, số trang) |
| `search_notes` | Tìm nội dung nằm ở đâu, hỏi bằng ngôn ngữ nào cũng được |
| `read_note` | Đọc trọn một trang hoặc khoảng trang của note |
| `search_pdf` | Tìm trong MỘT file PDF người dùng nêu tên |
| `ingest_document` | Nạp file mới (pdf/docx/md/ảnh) vào Vault |

Các tool phụ (`pdf_info`, `ocr_pdf`, `ocr_image`, `ocr_docx`, `export_page_images`, `batch_ingest`)
mặc định ẩn để model nhỏ ít chọn nhầm. Bật lại bằng cách thêm `"VISION_DOCS_ALL_TOOLS": "1"`
vào `env` của `vision-docs` trong `~/Library/Application Support/Jan/data/mcp_config.json`, rồi restart Jan.

Quy trình model cần theo: `list_notes` → `search_notes` (truyền nguyên câu hỏi, không dịch hay rút gọn) →
`read_note` quanh hit tốt nhất → trả lời kèm tên tài liệu và số trang.

## Chạy offline

Lúc chạy, stack không gọi internet: OCR bằng Vision, embedding bằng `llama-server` + file bge-m3 local
(bind `127.0.0.1`), web UI bind `127.0.0.1`, font nằm trong `static/fonts/`, sync ghi thẳng SQLite của Jan.
Kết quả đã kiểm bằng cách chặn mọi host ngoài `127.0.0.1`. Ngoại lệ duy nhất thấy được là bản thân Jan
ping `apps.jan.ai` để kiểm tra update.

Cài trên máy không có mạng:

1. Trên máy có mạng (cùng macOS, arch, bản Python): `.venv/bin/pip download -r requirements.txt -d wheels`, và đặt `bge-m3-Q8_0.gguf` vào `models/`.
2. Copy cả thư mục sang máy đích và chạy `bash install.sh`. Thấy `wheels/` thì pip cài `--no-index`, thấy model thì bỏ qua bước tải.
3. `llama.cpp` cần có sẵn (`llama-server` trong PATH hoặc `/opt/homebrew/bin`).

Tự kiểm tra các kết nối ra ngoài của Jan và stack này trong N giây (mặc định 120), trong lúc đó mở Jan và hỏi thử:

```bash
bash offline_check.sh 120
```

Mục `External` để trống nghĩa là không có kết nối ra ngoài máy.

## Tìm kiếm hoạt động thế nào

`search_notes` chạy theo thứ tự:

- Khớp nguyên văn cụm từ trước.
- Sau đó hybrid search trên index SQLite bền vững (`~/Library/Application Support/jan-vision-docs/index.sqlite`, đổi bằng env `PDF_OCR_INDEX`): vector bge-m3 (cross-language) + BM25 (FTS5), gộp bằng RRF. Index dựng lúc ingest (web UI) hoặc lần search đầu, chỉ cập nhật note đổi `(mtime, size)`; mỗi query chỉ tốn 1 embedding.
- Ngưỡng semantic: `SEM_STRONG = 0.60`, `SEM_LOW = 0.52` (dưới `SEM_LOW` coi như không có). Hiệu chỉnh trên vault thật: query rác hoặc lạc đề tối đa 0.566, query có nội dung thật từ 0.67 trở lên.
- Kết quả BM25 chỉ được tin khi mọi từ khoá đều có trong index và chunk phủ ≥ 60% trọng số IDF của query (từ hiếm như tên riêng bắt buộc có, âm tiết phổ biến nhẹ). Mode `hybrid` khi cosine ≥ `SEM_STRONG` hoặc có chunk BM25 đáng tin; `semantic-low` khi chỉ qua `SEM_LOW`.
- YAML frontmatter bị loại khỏi index; excerpt cắt theo đoạn, heading `## Trang N` hoặc `---`.
- `search_pdf`: nếu PDF đã nạp thì dùng lại note trong Vault (nhanh, hybrid); chưa nạp thì OCR rồi khớp nguyên văn (~0.5s/trang). Không tìm được file thì tự rơi về `search_notes`.

## Xử lý sự cố

- **Jan tìm không ra gì dù note đã sync**: Jan còn dùng MiniLM. Chạy `jan_sync.py --embed-check`, làm lại bước đổi embedding model.
- **"File not found" với file attach trong chat**: attachment inline của Jan không phải file trên đĩa, model chỉ nhận được tên. Server dò theo tên file (Downloads, Desktop, Documents, data của Jan, Spotlight) và nếu vẫn không thấy thì `search_pdf` rơi về `search_notes`. Với PDF scan, nạp bằng web UI thay vì attach inline.
- **PDF có mật khẩu**: OCR báo lỗi. Mở bằng Preview, export lại không mật khẩu.
- **Jan xoá project nhưng file DB còn trên đĩa**: danh sách project đọc từ localStorage của Jan (key `thread-management`), DB không có trong đó hiện ở mục "Đã xoá trong Jan". Bấm `Xoá file` (2 lần để xác nhận), file chuyển vào `~/.Trash`; server từ chối nếu project còn thread tham chiếu.
- **Model gọi sai tool**: kiểm tra đã dán prompt chưa, và mở thread mới.

## Gỡ cài đặt

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.jan.visdocs.webui.plist
rm ~/Library/LaunchAgents/ai.jan.visdocs.webui.plist
# nếu đã cài --with-watch:
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.jan.visdocs.watch.plist
rm ~/Library/LaunchAgents/ai.jan.visdocs.watch.plist
```

Xoá entry `vision-docs` trong `~/Library/Application Support/Jan/data/mcp_config.json` (có file `.bak` do installer tạo),
rồi xoá thư mục repo. Vault không bị đụng tới; xoá tay nếu không cần.

## Cấu trúc thư mục

```
jan-vision-docs-mcp/
├── install.sh              # cài đặt một lệnh
├── install_webui.sh        # launchd agent cho web UI
├── install_watch.sh        # watcher tuỳ chọn (--with-watch)
├── patch_jan_config.py     # ghi server vào mcp_config.json
├── server.py               # MCP server (stdio), định nghĩa tool
├── server_extra.py         # ingest, list/read/search_notes, embedding
├── webui.py                # web UI (FastAPI, 127.0.0.1)
├── jan_sync.py             # sync note vào project DB của Jan
├── offline_check.sh        # kiểm tra kết nối ra ngoài
├── prompts/jan-assistant-instructions.md
├── static/fonts/           # font đóng gói sẵn
├── requirements.txt
├── models/                 # bge-m3 (không commit)
└── wheels/                 # tuỳ chọn, cài offline (không commit)
```

Biến môi trường giữ tiền tố `PDF_OCR_*` (`PDF_OCR_VAULT`, `PDF_OCR_LANGS`, `PDF_OCR_WEBUI_PORT`, `PDF_OCR_EMB_MODEL`).
