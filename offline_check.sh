#!/usr/bin/env bash
# offline_check.sh — theo dõi mọi kết nối mạng RA NGOÀI máy của Jan + stack
# vision-docs (MCP server, web UI, llama-server) trong N giây.
# Dùng: bash offline_check.sh [giây=120]   (trong lúc đó mở Jan, hỏi thử vài câu)
# Kết quả: danh sách host:port bên ngoài mà từng tiến trình đã kết nối.
# Không in gì ở mục "External" = stack chạy offline được.
set -euo pipefail
DUR="${1:-120}"
PAT='Jan.app|Jan-Desktop|jan-llama|llama-server|jan-vision-docs-mcp/(server|webui|jan_sync)\.py'
OUT="$(mktemp -t offline_check)"
echo "Theo dõi ${DUR}s — mở Jan và hỏi thử vài câu dùng tool vision-docs…"
end=$(( $(date +%s) + DUR ))
while (( $(date +%s) < end )); do
  for pid in $(pgrep -f "$PAT" || true); do
    lsof -nP -a -p "$pid" -i 2>/dev/null | awk 'NR>1 && $8 ~ /TCP|UDP/ {print $1, $9}' >> "$OUT" || true
  done
  sleep 1
done
echo
echo "== Tiến trình đã thấy =="
pgrep -fl "$PAT" | cut -c1-120 || echo "(không thấy tiến trình nào — Jan có đang mở không?)"
echo
echo "== External (ngoài 127.0.0.1 / ::1) =="
grep -- '->' "$OUT" | grep -vE -- '->(127\.0\.0\.1|\[::1\]|localhost)' | sort | uniq -c | sort -rn || true
echo
echo "== Chỉ localhost (bình thường) =="
grep -- '->' "$OUT" | grep -E -- '->(127\.0\.0\.1|\[::1\]|localhost)' | awk '{print $1}' | sort | uniq -c || true
rm -f "$OUT"
