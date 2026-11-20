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
- Sau đó semantic search bằng bge-m3 (cross-language, câu tiếng Việt tìm được đoạn tiếng Anh), kết hợp xếp hạng trang theo keyword.
- Ngưỡng semantic: `SEM_STRONG = 0.60`, `SEM_LOW = 0.52` (dưới `SEM_LOW` coi như không có). Hiệu chỉnh trên vault thật: query rác hoặc lạc đề tối đa 0.566, query có nội dung thật từ 0.67 trở lên.
- Keyword mode: trang phải phủ ≥ 60% trọng số IDF của query (từ hiếm như tên riêng bắt buộc có, âm tiết phổ biến như "doanh thu", "năm" nhẹ); tokenizer nhận cả số (`12,8`, `18%`, `2026`).
- YAML frontmatter bị loại khỏi index; excerpt cắt theo đoạn, heading `## Trang N` hoặc `---`.
- `search_pdf`: nếu PDF đã nạp thì dùng lại note trong Vault (nhanh, semantic); chưa nạp thì OCR rồi khớp nguyên văn (~0.5s/trang). Không tìm được file thì tự rơi về `search_notes`.

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
