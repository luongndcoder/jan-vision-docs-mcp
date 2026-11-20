# Nhiệm vụ

Review UX của web UI chạy localhost (`webui.py`). Toàn bộ HTML/CSS/JS nằm trong một Python string `PAGE`. Mục tiêu: giao diện phải đúng ở cả desktop (1440px) lẫn mobile (390px), ở cả light và dark, và `/note` preview không hiển thị ký tự escape thừa.

# Đã sửa gì

1. Sửa dòng CSS bị dán chồng lên nhau: `.jb{font:...;border:...;.jb{display:inline-flex;...}` thành hai rule riêng biệt.
2. `/note/<name>` không còn `html.escape()` ở server. Server trả raw `text/plain`, client escape đúng một lần khi render. Trước đó `&quot;` và `&#x27;` lộ ra trong preview.
3. Media query mobile có hiệu lực trở lại. Sau khi sửa, các lỗi layout mobile vốn bị che giấu lộ ra và được xử lý: top bar dùng `minmax(0,1fr)`, `overflow-x:auto`, ẩn label; note row chia thành 3 dòng.
4. Selector `aside{}` đổi thành `#shell>aside{}` để không ăn vào `#panel` (preview drawer cũng là `<aside>`).
5. Tự chọn project khi chỉ có đúng 1 project đang live và user chưa từng chọn (key `localStorage` không tồn tại).
6. Cột name dùng `minmax(0,1fr)` thay vì `1fr 2fr`.
7. Panel `visibility:hidden` khi đóng, để bóng đổ không rò ra mép phải.
8. Badge JS chỉ hiện khi có lỗi. Đường dẫn vault rút gọn thành `~`. Thêm ô search cho library. Header sticky có nền đặc (gradient cũ làm row lộ xuyên qua). `#pview` có `overflow-wrap:anywhere`.

Verify: Playwright (playwright-core + Chrome hệ thống) chụp ảnh ở 1440 và 390, light và dark. Kiểm tra computed style: số `cssRules`, `gridTemplateColumns`, `scrollWidth > innerWidth` (có scroll ngang hay không), màu của `.jb.on`.

# Vấn đề thực sự

Nguyên nhân gốc là một lỗi copy-paste: hai dòng CSS bị nối liền, không có `}` ở giữa.

```css
.jb{font:500 11.5px var(--mono);color:var(--faint);border:1px solid var(--line);.jb{display:inline-flex;...}
```

Chrome hiện đại hỗ trợ CSS nesting gốc. Khi gặp `.jb{` bên trong block của `.jb` ngoài, parser coi đó là một nested rule. Block ngoài không bao giờ đóng đúng cách, nên nó nuốt mọi rule phía sau cho tới `</style>`.

Bằng chứng: `document.styleSheets[1].cssRules` có rule cuối cùng là `.jb`. Tức là toàn bộ rule sau đó không tồn tại trong stylesheet.

Triệu chứng nhìn từ ngoài rất khó hiểu:
- Media query mobile không bao giờ áp dụng, nên sidebar 236px chiếm 60% màn hình 390px.
- Trạng thái `.jb.on` ("✓ Jan") không có màu xanh.
- Màu toast và hover biến mất.

Không có lỗi nào hiện ra vì CSS parse lỗi là im lặng. Nhìn từng selector riêng lẻ thì mọi thứ trông đúng, vì vấn đề nằm ở cấu trúc parse chứ không ở nội dung rule.

Lỗi thứ hai là double-escape. Server gọi `html.escape()`, client lại escape thêm lần nữa (`escH` + `textContent`). Kết quả là escape hai lớp. Gốc của lỗi là đặt lớp escape ở sai chỗ, không phải do hàm escape sai.

# Tại sao chọn cách này

- Tách dòng bị dán chồng thành hai rule là sửa tại nguồn. Không cần thêm workaround cho `.jb` hay viết lại selector.
- Escape một lần, ở đúng biên output (client khi render DOM). Server trả dữ liệu thô vì `text/plain` không được hiểu là HTML. Cách này tránh việc mỗi lớp tự escape một lần và cộng dồn.
- Scope `aside` thành `#shell>aside` vì selector quá rộng là một rủi ro thật: nó vô tình áp style lên component khác có cùng tag.
- Verify bằng computed style và `cssRules` thay vì chỉ nhìn ảnh chụp. Ảnh chụp không cho biết một rule có được parse hay không.

# Hình dạng production

Trong một app thật, CSS nên nằm trong file `.css` riêng hoặc qua build step, để linter và build tool bắt lỗi cú pháp trước khi chạy. Ở đây CSS nằm trong string Python nên không có bước kiểm tra nào. Lỗi chỉ lộ ra khi mở trình duyệt.

Với một UI nhỏ chạy localhost, mức đầu tư hợp lý là: giữ `PAGE` như hiện tại, nhưng thêm một test kiểm tra cấu trúc stylesheet (xem phần "Lần sau nên nghĩ thế nào"). Không cần kéo cả build pipeline vào.

# Các cách khác khả thi

1. Tách CSS ra file `static/webui.css`, serve qua FastAPI `StaticFiles`. Phù hợp khi giao diện lớn lên, nhiều người sửa cùng lúc, hoặc muốn dùng linter như `stylelint`. Lúc đó lỗi cú pháp có công cụ bắt sớm.
2. Giữ CSS trong `PAGE`, thêm test Playwright hoặc test Python chạy trong CI: kiểm tra `document.styleSheets[...].cssRules` và assert rule cuối cùng là media query. Phù hợp khi muốn giữ kiến trúc single-file nhưng vẫn có lưới an toàn.
3. Dùng một bước validate nhẹ (ví dụ parse CSS bằng thư viện Python như `tinycss2`) trong test để bắt dấu `{` mất cân bằng trước khi chạy.

# Tại sao không chọn các cách đó

- Cách 1 là đúng về lâu dài, nhưng thay đổi kiến trúc giữa một đợt sửa UX là làm quá tay. Đây là một file localhost, không phải sản phẩm nhiều người dùng.
- Cách 2 là lựa chọn được ưu tiên ngắn hạn vì chi phí thấp và bắt đúng loại lỗi này. Chưa làm trong đợt này vì nằm ngoài phạm vi review UX.
- Cách 3 bắt được lỗi ngoặc, nhưng không bắt được lỗi "parse hợp lệ nhưng sai nghĩa" như nesting. Nên không đủ để thay thế cách 2.

# Khái niệm cốt lõi cần nắm

- CSS nesting: từ khi Chrome hỗ trợ native nesting, một rule bên trong block khác có thể là nested rule thay vì là lỗi. Vì vậy `.a{ x:1; .b{ y:2 } }` không còn "lỗi cú pháp" theo nghĩa cũ, nó là hợp lệ.
- Hệ quả của parse nested: một `{` thừa không tạo ra lỗi. Nó làm block bên ngoài kéo dài và nuốt rule sau đó. Mất style thường xuất hiện ở xa vị trí gây lỗi.
- CSS fail silently: trình duyệt bỏ qua declaration hoặc rule không hiểu, không log lỗi ra console. Muốn biết rule nào thực sự tồn tại phải hỏi DOM: `document.styleSheets[i].cssRules`.
- Escape đúng lớp: escape ở nơi chuyển dữ liệu sang ngữ cảnh khác (text vào HTML DOM). Escape ở mọi tầng sẽ tạo ra double-escape như `&amp;quot;`.
- Media query không áp dụng có thể không phải lỗi media query. Nếu rule đó không được parse, mọi thứ phía sau cũng mất theo.

# Lỗi thường gặp

- Nhìn chằm chằm vào từng selector để tìm lỗi, trong khi lỗi nằm ở ranh giới giữa hai rule. Đây là điều đã xảy ra trong đợt này: triệu chứng hiện ở `.jb`, `#panel`, media query, nhưng gốc nằm ở một dòng khác.
- Tin rằng "không có lỗi trong console" nghĩa là CSS ổn. CSS không báo lỗi.
- Sửa từng triệu chứng riêng (thêm `!important`, đổi selector) thay vì đếm số rule. Mỗi lần sửa một triệu chứng tạo thêm code nhưng không xử lý được nguyên nhân.
- Quên rằng `aside` là tag chung. Một selector theo tag sẽ ăn vào mọi phần tử cùng tag, kể cả drawer.
- Escape hai lần rồi nghĩ là server bug, trong khi cả hai lớp đều đang escape.

# Ví dụ nhỏ

Kiểm tra nhanh trong DevTools console khi thấy style "biến mất":

```js
const s = document.styleSheets[1];
console.log(s.cssRules.length);
console.log(s.cssRules[s.cssRules.length - 1].selectorText);
// Nếu in ra ".jb" thay vì media query cuối file, rule đã bị nuốt.
```

Nếu rule cuối là `.jb` trong khi file có media query ở cuối, nghĩa là có một `{` thừa ở trước đó. Tìm ngược lên dòng có `.jb{` xuất hiện hai lần.

Test hồi quy đề xuất (Playwright, chạy trong CI):

```js
const last = await page.evaluate(() => {
  const rules = [...document.styleSheets[1].cssRules];
  return rules[rules.length - 1].conditionText || rules[rules.length - 1].selectorText;
});
expect(last).toContain('max-width');
```

# Lần sau nên nghĩ thế nào

- Khi một nhóm style biến mất cùng lúc, đừng debug từng selector. Đếm `cssRules` hoặc nhìn rule cuối cùng được parse trước.
- Coi CSS trong string như code: có thể bị hỏng cấu trúc mà không báo lỗi. Thêm một assertion về cấu trúc (số rule, rule cuối) vào test.
- Khi thấy hai dòng CSS nối liền nhau trong một diff, đó là dấu hiệu lỗi copy-paste. Kiểm tra ngay trong diff trước khi đi tìm nguyên nhân ở chỗ khác.
- Một lớp escape duy nhất, đặt ở biên output. Khi thấy `&amp;` hoặc `&quot;` lộ ra, hỏi ngay "dữ liệu đã được escape ở bao nhiêu chỗ".
