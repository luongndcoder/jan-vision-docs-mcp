# Nhiệm vụ

Thay pipeline `search_notes` hiện tại (mỗi query re-embed ~137 window của top page rồi ghi lại cache JSON 4.7MB) bằng persistent hybrid index trong SQLite: vector + FTS5 BM25, fuse bằng RRF. Ràng buộc: xây eval trước khi tune, và không làm hỏng các câu positive đã đúng.

# Đã sửa gì

1. Dựng eval trước: 40 câu (12 vi_para, 6 en_para, 5 exact, 5 keyword, 4 vi_native, 8 negative), mỗi câu positive có file + page đã kiểm chứng. `eval/run_eval.py` báo hit@1/3/6, MRR, negative pass/low/fail, p50/p95, và `--compare` để bắt regression.
2. Chuyển công việc từ query time sang ingest time. `search_index.py` lưu vào `~/Library/Application Support/jan-vision-docs/index.sqlite` gồm: bảng `chunks` (cùng cửa sổ 4 dòng), vector float32 đã L2-normalise, FTS5 external content với `unicode61 remove_diacritics 0`, và `fts5vocab` để lấy document frequency.
3. Build incremental theo `(mtime, size)`. Dùng WAL và `BEGIN IMMEDIATE` để MCP server và web UI cùng ghi được.
4. Query: 1 embedding, numpy dot trên ma trận cache trong bộ nhớ, BM25 top 30, trust gate, RRF k=60, tối đa 2 chunk mỗi page.

Kết quả so với baseline:

| Chỉ số | Baseline | Sau khi sửa |
|---|---|---|
| hit@1 / MRR (positive) | 97% / - | 97% / 0.98 (không đổi) |
| negative pass | 25% | 62% |
| negative FAIL | 38% | 0% |
| latency p50 / p95 | 2.1s / 6.2s | 0.03s / 0.03s |

Sửa được q35, q37, q38; không câu nào bị hỏng. Build index ~55s cho 5 note (3622 chunk, 17.6MB).

# Vấn đề thực sự

- Vault trộn tiếng Việt và tiếng Anh, phần lớn page là tiếng Anh. Từ tiếng Việt phổ biến như "kinh doanh", "quý" hiếm trong corpus nên IDF bị thổi phồng, và một từ chung chung có thể kéo kết quả keyword đi sai.
- Negative FAIL: entity "Coteccons" không có trong vault, nhưng keyword tier vẫn match nhờ các term khác. Thiếu một kiểm tra "term tuyệt đối vắng" nên keyword vẫn được tin.
- Chi phí query là vấn đề kiến trúc, không phải thuật toán: re-embed window mỗi lần query và rewrite JSON. Dưới memory pressure (model 4B của Jan, context 65k) một batch 16 mất 1.6s.
- Ngưỡng cosine đã chạm giới hạn: true positive 0.586, negative 0.541–0.575, margin chỉ 0.011. Tune thêm chỉ là overfit 40 câu.

# Tại sao chọn cách này

- Eval trước vì không có eval thì mọi "cải thiện" chỉ là cảm giác. Eval tách riêng positive và negative nên thấy được trade-off.
- Index ở ingest: query còn 1 embedding + một phép nhân ma trận + một truy vấn FTS5. p95 từ 6.2s xuống 0.03s.
- BM25 + vector + RRF là tổ hợp chuẩn, không cần trọng số tune trên 40 câu. RRF chỉ dùng thứ hạng nên không cần chuẩn hoá cosine và BM25 về cùng thang đo.
- Trust gate là heuristic nhưng có cơ sở logic: nếu term tuyệt đối vắng khỏi vault thì keyword không có bằng chứng.

# Hình dạng production

- Một file SQLite ở chế độ WAL, `busy_timeout` 10s, dùng chung cho MCP server và web UI.
- `ensure()` chạy khi ingest. `search()` gọi `ensure(budget_s=60)` để bắt file mới mà không block quá lâu; nếu hết budget thì search vẫn chạy với index một phần.
- Schema: `notes(path, mtime, size)`, `chunks(id, path, page, ord, text, vec)`, `chunks_fts` (external content), `chunks_vocab`.
- Ma trận vector cache theo process, key `(db_path, MAX(id), COUNT(*))`.
- Đã biết: hai process build đồng thời cho DB nhất quán (không trùng, integrity ok) nhưng làm việc hai lần. Chấp nhận được ở quy mô hiện tại.

# Các cách khác khả thi

1. Giữ JSON cache nhưng chỉ re-embed chunk đổi và lưu embedding theo chunk. Hợp khi vault nhỏ và không muốn thêm DB. Nhược điểm: vẫn load toàn bộ JSON vào RAM và không có BM25 thật.
2. Vector DB chuyên dụng (LanceDB, Chroma, sqlite-vec) với ANN. Hợp khi số chunk lên hàng triệu hoặc cần filter vector phức tạp.
3. Reranker cross-encoder sau retrieval. Hợp khi top-k đã có positive nhưng thứ tự sai, hoặc khi ngưỡng không còn tách được positive và negative.

# Tại sao không chọn các cách đó

- Cách 1: vẫn là vấn đề đang sửa (rewrite và load cache mỗi lần), và không có BM25.
- Cách 2: với 3622 vector, brute-force numpy đã đạt p95 0.03s. Thêm dependency không đổi được gì ở quy mô này.
- Cách 3: chưa làm. Ngưỡng hiện tại vẫn tách được phần lớn negative; reranker là bước kế tiếp khi margin 0.011 không đủ. Làm bây giờ với 40 câu là gold-plating.

# Khái niệm cốt lõi cần nắm

- IDF = log((N+1)/(df+1)): term hiếm có trọng số cao. Corpus lẫn ngôn ngữ làm phân bố df lệch, nên IDF không còn phản ánh "độ quan trọng" như mong đợi.
- FTS5 external content: `chunks_fts` không lưu text, đọc qua rowid từ `chunks`. Khi xoá phải `INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', ...)` với đúng text cũ, nếu không index lệch.
- `fts5vocab` mode `row`: cho df theo term, dùng để tính IDF mà không tự đếm.
- RRF: score = Σ 1/(k + rank), k = 60. Ví dụ: chunk đứng hạng 1 ở vector và hạng 2 ở BM25 có score 1/61 + 1/62 ≈ 0.0325.
- Trust gate (điều kiện logic) và ngưỡng cosine (điều kiện thống kê) là hai loại khác nhau; không nên dùng một loại để sửa lỗi của loại kia.

# Lỗi thường gặp

- `remove_diacritics 0` nghĩa là "quý" và "quy" là hai token khác nhau. Query gõ không dấu sẽ không match chữ có dấu. Đây là trade-off đã chọn và chưa được eval đo riêng.
- Cache key `(MAX(id), COUNT(*))` là **bug thật** (đã tái hiện): re-index note giữ rowid cao nhất với cùng số chunk → SQLite cấp lại đúng các rowid cũ → key không đổi → MCP server (process sống lâu) tìm trên vector cũ, query nội dung mới ra 0 kết quả. Đã sửa: key thêm fingerprint `notes(COUNT, TOTAL(mtime), TOTAL(size))` — mọi lần (re)index đều đổi fingerprint. Bài học: cache key phải đổi theo MỌI thay đổi dữ liệu, không chỉ theo kích thước; rowid không AUTOINCREMENT bị tái sử dụng.
- Tune ngưỡng trên 40 câu rồi báo cáo như kết quả tổng quát.
- Sửa heuristic mà không chạy `--compare`: có thể fix negative nhưng làm hỏng positive không thấy.

# Ví dụ nhỏ

Trust gate chặn keyword khi có term tuyệt đối vắng:

```python
terms = ["coteccons", "doanh"]
df = {"coteccons": 0, "doanh": 120}   # Coteccons không có trong vault
if all(df[t] > 0 for t in terms):      # False -> nhánh BM25 bị bỏ qua
    ...
```

Chạy eval trước và sau khi đổi:

```bash
.venv/bin/python eval/run_eval.py --save
.venv/bin/python eval/run_eval.py --compare eval/results/<ts>.json
```

# Lần sau nên nghĩ thế nào

1. Eval trước, tune sau. Ghi baseline trước khi đổi bất cứ thứ gì.
2. Hỏi: việc này có cần làm lại mỗi query không? Nếu có, chuyển sang ingest time.
3. Dùng kỹ thuật chuẩn (BM25 + vector + RRF) trước khi viết heuristic tự chế.
4. Khi margin giữa positive và negative nhỏ đến mức tune không còn tách được, dừng tune và chuyển sang reranker.
5. Mỗi guard mới phải được kiểm chứng: chặn được đúng case (Coteccons) và không làm hỏng positive (`--compare`).
