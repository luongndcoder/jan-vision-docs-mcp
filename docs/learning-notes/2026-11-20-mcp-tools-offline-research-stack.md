# Nhiệm vụ

Xây stack nghiên cứu tài liệu chạy hoàn toàn offline: Jan desktop với model local 4B, một MCP server local (Apple Vision OCR, Vault các note `.md`, embedding bge-m3 qua `llama-server` trên `127.0.0.1`) và web UI bằng FastAPI.

Ràng buộc: model 4B chọn tool kém khi có nhiều tool, PDF lớn phải trả kết quả trong tool timeout 180 s của Jan, và không được có network call ra ngoài.

# Đã sửa gì

- Gom tool từ 9 xuống 5 mặc định: `list_notes`, `search_notes`, `read_note`, `search_pdf`, `ingest_document`. Các tool còn lại giữ sau env `VISION_DOCS_ALL_TOOLS=1` qua decorator `_extra_tool`. Schema + description giảm từ ~9.2k xuống ~5k ký tự.
- Thêm `read_note(file, first_page, last_page)`: đọc section `## Trang N` của note, không cần OCR, output tối đa 12k ký tự. Resolve theo exact name, tên `.pdf` gốc hoặc substring duy nhất; nếu mơ hồ thì trả danh sách ứng viên. Đường dẫn ra ngoài vault (`../../.ssh/id_rsa`) bị từ chối.
- `list_notes` làm danh mục để model không bịa tên file (trước đây model gọi `search_pdf("document.pdf")`).
- `search_pdf` tái dùng note trong Vault qua `find_note_for_source` (khớp frontmatter `source_path` hoặc tên source; bỏ qua nếu PDF mới hơn note). Từ ~97 s (178 trang × 0.54 s/trang) xuống 2.8 s, có lọc page range.
- Calibrate ngưỡng semantic trên vault thật: `SEM_STRONG` 0.50 → 0.60, `SEM_LOW` 0.45 → 0.52. Junk/off-topic đạt tối đa 0.566 ("giá vé máy bay" khớp đoạn IELTS về du lịch; "lorem ipsum" 0.517); query thật ≥ 0.67.
- Keyword tier yêu cầu ≥ 60% số từ của query khớp. Âm tiết ngắn tiếng Việt ("công", "giá") substring-match ở khắp nơi, và query 2 từ trước đây giữ cả page có 0 match.
- Output mặc định 6 hit, excerpt 700–900 ký tự; query "travel" giảm từ 19.6k xuống 4.8k ký tự.
- Offline: Google Fonts trong web UI (call ngoài duy nhất trong code) được vendor vào `static/fonts` (OFL). Playwright chặn mọi host không phải 127.0.0.1 và thấy 0 request ngoài.
- `offline_check.sh` (poll `lsof` 3 phút khi Jan đang chạy): 700+ connection, tất cả localhost, trừ 1 kết nối Jan-Desktop → 104.26.2.30:443 mà `dig` map về apps.jan.ai (update check).
- `install.sh` có offline mode: `wheels/` + `pip --no-index`.
- Vault hygiene: nút xóa note chuyển `.md` và `Attachments` vào `~/.Trash`, đồng thời gọi `jan_sync.remove_note` trên mọi project. "Gỡ khỏi Jan" (`/jan-unsync`) là action riêng.
- `vault_index.json` không bao giờ bỏ note đã xóa, nên search có thể trả tài liệu đã xóa. Nay prune các path không còn tồn tại.
- Bug có sẵn: PDF có mật khẩu làm `pdf_info` crash. `pypdf.decrypt("")` trả về 0 thay vì raise; Quartz sau đó không trả trang nào ("Page 1 does not exist"). Fix: kiểm tra `CGPDFDocumentIsEncrypted`/`IsUnlocked` trong `_load_pdf` và raise thông báo rõ.
- Prompt Jan assistant bỏ hai chỉ dẫn sai: "dịch keyword sang tiếng Anh trước" (mâu thuẫn với semantic search đa ngôn ngữ) và "dữ liệu real-time" (mâu thuẫn với offline). Prompt mới: `prompts/jan-assistant-instructions.md`.

# Vấn đề thực sự

Không phải thiếu tính năng. Có ba vấn đề gốc:

1. Model nhỏ bị quá tải lựa chọn: nhiều tool và mô tả dài làm chọn sai tool, bịa tham số.
2. Đường đi tới nội dung PDF đắt: OCR lại toàn bộ mỗi lần gọi, vượt timeout của client. Đây là lỗi kiến trúc, không phải lỗi model.
3. Ngưỡng và heuristic chọn theo cảm tính (không có dữ liệu tiếng Việt thật) nên lọc sai. Prompt và config còn mang giả định online (dịch sang Anh, real-time) mâu thuẫn với mục tiêu.

"Offline" là thuộc tính phải đo. Grep code không thấy call ngoài, nhưng `lsof` vẫn thấy Jan check update.

# Tại sao chọn cách này

- Thu hẹp tool set thay vì viết prompt dài hơn: giảm số lựa chọn trực tiếp, không phụ thuộc việc model có "nghe lời" hay không.
- Dùng lại note đã OCR (Vault là cache sẵn có) thay vì tạo cache mới. Note đã có `## Trang N` nên `read_note` và page filter dùng luôn.
- `list_notes` và lỗi ambiguous có cấu trúc (kèm candidates): model không phải đoán tên, tự sửa được.
- Calibrate trên vault tiếng Việt thật: đo phân phối score của junk và real, đặt ngưỡng vào khoảng trống giữa.
- Đo network bằng Playwright chặn host và `lsof`/`dig` thay vì đọc config.

# Hình dạng production

- Jan gọi MCP server local, tool timeout 180 s. Mọi thành phần chạy trên máy: model 4B trong Jan, `llama-server` bge-m3 ở `127.0.0.1`, Apple Vision OCR qua Quartz.
- Vault `.md` là nguồn sự thật; `vault_index.json` là cache embedding, có prune.
- Web UI FastAPI: asset và font nằm trong repo, không gọi CDN.
- Cài offline: `wheels/` + `pip --no-index`.
- Kiểm tra offline là bước vận hành: chạy `offline_check.sh` khi Jan đang dùng, và Playwright chặn host ngoài khi thay đổi web UI.
- Chưa xử lý: update check của Jan vẫn ra apps.jan.ai. "Offline hoàn toàn" cần tắt update check trong Jan, hoặc ghi rõ đây là ngoại lệ.

# Các cách khác khả thi

1. Giữ nhiều tool, thêm router phía server (rule hoặc classifier): hợp khi model đủ lớn (ví dụ từ ~14B) hoặc cần đủ chức năng cùng lúc. Đổi lại là thêm một lớp phải bảo trì.
2. Vector store riêng cho PDF (chunk + embed từng trang lúc ingest): hợp khi corpus lớn và cần query xuyên nhiều tài liệu nhanh. Đổi lại là đồng bộ hai index (Vault và vector).
3. OCR đa nền tảng (Tesseract, PaddleOCR): hợp khi cần chạy ngoài macOS. Phải đo lại chất lượng tiếng Việt và tốc độ.

# Tại sao không chọn các cách đó

- Cách 1: với 4B, router cũng là model hoặc rule phải đúng; thêm lớp là thêm điểm lỗi. Giảm tool trực tiếp hơn.
- Cách 2: corpus hiện tại là tài liệu cá nhân, Vault đã đủ. Chưa có bằng chứng về nhu cầu xuyên tài liệu, nên là over-engineering.
- Cách 3: Apple Vision cho kết quả tốt và nhanh trên máy hiện tại. Đổi OCR là thay đổi lớn, không có số đo nào ủng hộ.

# Khái niệm cốt lõi cần nắm

- **Tool budget của model nhỏ**: số tool và độ dài mô tả là chi phí nhận thức, không chỉ là token. Đo bằng số ký tự schema và tỉ lệ chọn đúng tool.
- **Cache hợp lệ có điều kiện**: `find_note_for_source` chỉ dùng note khi PDF không mới hơn. Cache không có invalidation (như `vault_index.json`) là nguồn bug.
- **Ngưỡng là phân phối, không phải hằng số**: đặt ngưỡng trong khoảng giữa junk max (0.566) và real min (0.67). Ngưỡng cũ 0.50 nằm trong vùng junk.
- **Substring với tiếng Việt**: âm tiết ngắn gây false positive, nên gate theo tỉ lệ từ khớp.
- **Lỗi im lặng**: `decrypt("")` trả 0 là thất bại nhưng không raise. Luôn kiểm tra trạng thái trước khi đọc trang.
- **Offline phải đo**: config chỉ cho biết ý định; `lsof`, `dig` và Playwright cho biết hành vi thực.
- **Prompt cũng là code**: chỉ dẫn "dịch sang tiếng Anh" có thể làm hỏng retrieval đa ngôn ngữ.

# Lỗi thường gặp

- Đặt ngưỡng theo dữ liệu tiếng Anh hoặc dữ liệu tổng hợp; đưa vào vault tiếng Việt thật thì junk lọt qua.
- Đếm tool theo cái đã đăng ký thay vì cái model thấy. Extra tool phải thật sự vắng mặt khỏi schema mặc định.
- Xóa file trên disk nhưng quên index/cache, nên search trả tài liệu đã xóa.
- Coi "không raise" là "thành công" với API như `decrypt()`.
- Đo offline bằng grep code: bỏ sót call từ binary bên thứ ba (update check).
- Đọc toàn bộ PDF trong một lần gọi có timeout ngắn: 178 trang × 0.54 s ≈ 97 s, sát 180 s khi máy tải nặng.
- Kiểm tra đường dẫn bằng prefix chuỗi thay vì resolve rồi so sánh, dễ bị path traversal.

# Ví dụ nhỏ

Gate keyword theo tỉ lệ từ (minh họa, không phải code thật):

```python
def keyword_hit(query_words: list[str], text: str) -> bool:
    matched = sum(1 for w in query_words if w in text)
    return matched / len(query_words) >= 0.6

keyword_hit(["giá", "vé", "máy", "bay"], "...vé tàu...")  # False: 1/4 < 0.6
```

Ngưỡng semantic đặt theo số đo:

```python
SEM_STRONG = 0.60  # junk max 0.566 nằm dưới; real min 0.67 nằm trên
SEM_LOW = 0.52     # 0.50 cũ nằm trong vùng junk
```

# Lần sau nên nghĩ thế nào

- Trước khi thêm tool hay prompt, hỏi: model nhỏ có chọn đúng không khi có 5 tool? Đo tỉ lệ chọn đúng trước và sau.
- Trước khi đặt ngưỡng, lấy dữ liệu thật đúng ngôn ngữ và đúng corpus; vẽ phân phối junk và real rồi chọn điểm giữa.
- Thứ gì tốn thời gian và lặp lại (OCR) thì hỏi có cache sẵn không, và cache có invalidation đúng không.
- Claim về "offline", "không gọi ngoài" phải có bằng chứng hành vi (lsof, dig, chặn host), không chỉ đọc code.
- Với mọi cache hoặc index, kiểm tra đường xóa: xóa nguồn thì cache có bị prune không.
- Mọi thứ mơ hồ (không tìm thấy, nhiều kết quả, file mã hóa) phải trả lỗi có cấu trúc để model tự phục hồi.
