# Nhiệm vụ

Sửa các vấn đề review phát hiện trong commit `c6e32d1` (semantic-first ranking của `search_notes`) ở `server.py` và `server_extra.py`. Mục tiêu: thứ tự ưu tiên semantic/keyword phải hợp lý, cold start không được chặn tìm kiếm, lỗi không bị nuốt, và excerpt phải tách đoạn đúng.

# Đã sửa gì

1. Semantic yếu (0.45–0.50) không còn đè keyword mạnh. Nó chỉ thắng khi `_kw_ratio < 0.5`; còn lại trả keyword kèm `semantic_low_results`.
2. Query không có keyword hữu ích (`là gì`, `ai`) không return sớm nữa; semantic vẫn chạy.
3. `_ensure_embed_server(wait=...)`: chỉ chờ server khi keyword không có kết quả (`wait_for_server=not scored`). Trường hợp còn lại spawn llama-server ở nền và trả keyword ngay. `_ES_PROC` giữ process handle để không spawn trùng.
4. Nhánh "không có kết quả" không gọi semantic lần 2; tái dùng `_sem`.
5. Exception được log ra stderr và trả trong field `semantic_error`. Khi semantic lỗi thì không đưa hint "absent".
6. `top_k=max(1, min(max_hits, 6))` thay cho giá trị cố định.
7. Docstring của `search_notes` (`server.py`) và `search_notes` (`server_extra.py`) được cập nhật theo đúng các `mode`.
8. Bug có sẵn: excerpt tách và nối bằng `"\\n\\n"` (backslash-n thật) thay vì `"\n\n"`, cùng dạng lỗi escape với commit `e358605`.

Verify: temp vault + mock `_semantic_search_notes`, bao phủ 6 nhánh.

# Vấn đề thực sự

Commit `c6e32d1` đảo thứ tự ưu tiên nhưng không định nghĩa ngưỡng so với keyword, nên semantic ngưỡng thấp đè keyword mạnh.

Semantic phụ thuộc một tiến trình ngoài (llama-server + bge-m3, cold start có thể tới 90s). Dependency này bị đưa vào đường tìm kiếm đồng bộ mà không có chính sách chờ riêng.

Nguy hiểm nhất là `except Exception: None`. Lỗi bị biến thành "không có kết quả", và LLM sẽ kết luận "nội dung không có trong tài liệu" trong khi thực tế là "chưa kiểm tra được".

# Tại sao chọn cách này

- Giữ keyword làm baseline, semantic là tín hiệu bổ sung có điều kiện. Cách này đơn giản, giải thích được qua field `mode`, không đổi contract tool.
- Chỉ chờ server khi keyword rỗng, vì đây là trường hợp duy nhất không có gì để trả. Trade-off: lần search đầu có keyword hit có thể chưa dùng được semantic, nhưng lần sau thì có.
- Lỗi đi ra payload (`semantic_error`) để caller biết không được kết luận "absent".
- `_ES_PROC.poll()` cho phép spawn lại nếu process đã chết, và tránh spawn trùng khi đang chạy.

# Hình dạng production

- Tool trả JSON với `mode` ổn định. Caller dựa vào `mode`, `semantic_error`, `semantic_low_results`.
- `server.py` là MCP stdio server nên stdout là kênh JSON-RPC. Log phải đi qua stderr.
- Thiếu file model hoặc binary llama-server thì `_ensure_embed_server` trả `False`, semantic trả `None`, không gây lỗi tool.
- Page vector đã cache theo mtime nên các lần gọi lặp lại nhanh.

# Các cách khác khả thi

1. **Chạy semantic song song với keyword rồi merge theo điểm**: hợp khi embed server luôn warm và latency là ưu tiên. Cần calibrate hai thang điểm.
2. **Eager start embed server khi MCP process khởi động**: hợp khi máy gần như luôn dùng tool này. Né được cold start thay vì xử lý nó, đổi lại tốn RAM/CPU ngay từ đầu.
3. **Tách thành tool riêng `semantic_search_notes`**: hợp khi muốn phân loại rõ hai kiểu tìm kiếm. Đổi lại LLM phải tự chọn tool và dễ chọn sai.
4. **Rerank**: keyword lấy candidate, bge-m3 chấm lại. Hợp khi keyword có recall tốt nhưng precision kém.

# Tại sao không chọn các cách đó

- Song song + merge: tỷ lệ từ khóa và cosine similarity không so sánh trực tiếp được. Chưa có bộ eval để calibrate.
- Eager start: tốn tài nguyên liên tục kể cả khi không search. Lỗi thực tế là logic ưu tiên và lỗi bị nuốt, không phải cold start.
- Tool riêng: đổi contract tool đang được Jan dùng, và chưa có bằng chứng LLM chọn sai thường xuyên.
- Rerank: thêm bước và dependency, trong khi vấn đề nằm ở ngưỡng và thứ tự ưu tiên, không phải recall.

# Khái niệm cốt lõi cần nắm

- **Hai thang điểm khác nhau**: keyword ratio (số từ khớp / số từ, rời rạc trong 0–1) và cosine similarity của bge-m3. Ngưỡng 0.50 và 0.45 chỉ có nghĩa trên thang cosine.
- **"Không có kết quả" khác "không kiểm tra được"**: phải tách hai trạng thái này trong payload trả cho LLM.
- **Chính sách chờ thuộc về caller**: `wait` là tham số của caller, không nên hard-code trong helper dùng chung.
- **Popen handle**: giữ reference để kiểm tra `poll()`, tránh spawn trùng và phát hiện process đã crash.
- **Escape trong source**: `"\\n\\n"` là hai ký tự backslash-n, còn `"\n\n"` là xuống dòng thật.

# Lỗi thường gặp

- `print()` debug trong MCP stdio server làm hỏng JSON-RPC. Dùng `sys.stderr`.
- Nuốt exception rồi trả `None`, khiến LLM kết luận sai là nội dung không tồn tại.
- Tưởng `_ensure_embed_server(wait=False)` sẽ có semantic ngay lần đầu. Thực tế lần đầu trả `False`, server khởi động nền cho các lần sau.
- Hard-code `top_k` và bỏ qua `max_hits`.
- Viết `"\\n\\n"` trong source khi muốn newline thật. Sau khi sửa bằng heredoc hoặc script cần grep lại escape.
- Áp ngưỡng cố định 0.45/0.50 mà không có eval để kiểm chứng.

# Ví dụ nhỏ

```python
text = "đoạn một\n\nđoạn hai"   # newline thật
text.split("\n\n")     # ['đoạn một', 'đoạn hai']
text.split("\\n\\n")   # ['đoạn một\n\nđoạn hai'], không tách được

# Gate sau khi sửa: _top = 0.48, 4 từ khóa, 3 từ khớp -> _kw_ratio = 0.75
# 0.48 < 0.50 nên không phải semantic mạnh;
# 0.48 >= 0.45 nhưng 0.75 >= 0.5 nên không phải semantic-low -> trả keyword-ranked
```

# Lần sau nên nghĩ thế nào

- Khi thêm nguồn tìm kiếm thứ hai, hỏi trước: hai thang điểm có so sánh được không, và ngưỡng dựa trên căn cứ nào?
- Mọi payload trả cho LLM phải phân biệt "không tìm thấy" với "không tìm được".
- Dependency ngoài (server, model) có chính sách chờ riêng. Đừng đặt `wait` trong helper dùng chung.
- Với MCP stdio, stdout là giao thức. Mọi log đi qua stderr.
- Sau khi sửa file bằng heredoc hoặc script, grep lại escape.
- Nên có một bộ query eval nhỏ để calibrate ngưỡng thay vì đoán.

# Cập nhật (cùng ngày, sau khi test thật trên Jan)

Quyết định "chỉ chờ embed server khi keyword không có kết quả" đã **sai** và bị revert ở commit `8047fcd`:

- Câu hỏi tiếng Việt dài gần như **luôn** có keyword hit rác (âm tiết phổ biến như "của", "các", "kinh tế" khớp substring khắp nơi). Vì vậy điều kiện "keyword có kết quả" gần như luôn đúng, nên semantic bị bỏ qua đúng vào lần cần nó nhất. Thực tế: query về "di chuyển… săn bắt… di cư" trả về Nowcasting 11/23 từ, thay vì IELTS 10 trang 66 (score 0.854).
- Fix: luôn chờ server. MCP server **prewarm** bge-m3 lúc khởi động (`_ensure_embed_server(wait=False)` trong `main()`), nên cold start chỉ còn khoảng 6s. Cách "eager start" bị loại ở trên hoá ra lại là cách đúng.
- Lỗi đi kèm: model nhỏ bịa tên file (`search_pdf("document.pdf", …)`). Fix: `search_pdf` gặp `FileNotFoundError` thì tự fallback sang `search_notes`, thay vì trả lỗi để model đi lạc.

Bài học: một heuristic về "tín hiệu đủ mạnh" (có keyword hit) phải được kiểm bằng dữ liệu thật của ngôn ngữ mục tiêu. Với tiếng Việt (âm tiết ngắn, lặp nhiều), số lượng hit gần như không mang thông tin.
