#!/bin/bash
# -*- coding: utf-8 -*-
# install.sh — one-shot installer for jan-pdf-ocr-mcp on any macOS machine.
#
# Cài:    bash install.sh
# Tuỳ chọn (env hoặc flag):
#   PDF_OCR_VAULT=/path/vault   bash install.sh         # vault vị trí khác
#   bash install.sh --no-jan    --no-watch    --no-model
#
# Yêu cầu trên máy đích:
#   - macOS 13+ (Apple Silicon khuyên dùng; Vision OCR cần 10.15+)
#   - python3 (Xcode CLT hoặc brew: brew install python)
#   - llama.cpp cho semantic search:  brew install llama.cpp
#     (không có thì server vẫn chạy, tier semantic tự tắt)
#   - Jan app (để patch mcp_config.json) — bỏ qua nếu --no-jan
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

VAULT="${PDF_OCR_VAULT:-$HOME/Documents/Vault}"
BAKE_SHA="950f4a8e5e19477a6d3c26d2f162233c20002c601f75e4b002e3239997821167"
BGE_URL="https://huggingface.co/gpustack/bge-m3-GGUF/resolve/main/bge-m3-Q8_0.gguf"
MODEL_CANDIDATES=(
  "$DIR/models/bge-m3-Q8_0.gguf"
  "$HOME/.local/share/jan-pdf-ocr/bge-m3-Q8_0.gguf"
  "$HOME/Library/Application Support/Jan/data/llamacpp/models/sentence-transformer-mini/model.gguf"
)

NO_JAN=0; NO_WATCH=0; NO_MODEL=0
for arg in "$@"; do
  case "$arg" in
    --no-jan) NO_JAN=1 ;; --no-watch) NO_WATCH=1 ;; --no-model) NO_MODEL=1 ;;
    *) echo "Flag lạ: $arg (dùng: --no-jan --no-watch --no-model)"; exit 1 ;;
  esac
done

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }

# --- 0. sanity -------------------------------------------------------------
[[ "$(uname -s)" == "Darwin" ]] || { echo "Chỉ hỗ trợ macOS."; exit 1; }
[[ "$(uname -m)" == "arm64" ]] || warn "Máy Intel: Vision OCR vẫn chạy, llama.cpp lập trình chậm hơn."
command -v python3 >/dev/null || { echo "Thiếu python3. Cài: xcode-select --install hoặc brew install python"; exit 1; }

# --- 1. Vault --------------------------------------------------------------
say "Vault: $VAULT"
mkdir -p "$VAULT/_dropbox" "$VAULT/Inbox" "$VAULT/Notes" "$VAULT/Attachments"

# --- 2. Venv + deps ---------------------------------------------------------
say "Tạo .venv và cài Python deps (~1-2 phút lần đầu)"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# --- 3. Embedding model (bge-m3 Q8, ~634MB) ---------------------------------
find_model() {
  for p in "${MODEL_CANDIDATES[@]}"; do [[ -f "$p" ]] && { echo "$p"; return 0; }; done
  return 1
}
if (( NO_MODEL )); then
  warn "Bỏ qua model (--no-model) — tier semantic sẽ tắt."
elif MODEL_PATH="$(find_model)"; then
  say "Đã có embedding model: $MODEL_PATH"
else
  say "Tải bge-m3-Q8_0.gguf (~634MB) về $DIR/models/"
  mkdir -p "$DIR/models"
  curl -L --fail --progress-bar -o "$DIR/models/bge-m3-Q8_0.gguf.part" "$BGE_URL"
  echo "$BAKE_SHA  $DIR/models/bge-m3-Q8_0.gguf.part" | shasum -a 256 -c - \
    || { rm -f "$DIR/models/bge-m3-Q8_0.gguf.part"; echo "SHA sai — file hỏng, xoá rồi thử lại."; exit 1; }
  mv "$DIR/models/bge-m3-Q8_0.gguf.part" "$DIR/models/bge-m3-Q8_0.gguf"
fi

# --- 4. llama.cpp -----------------------------------------------------------
if ! command -v llama-server >/dev/null && [[ ! -x /opt/homebrew/bin/llama-server ]]; then
  warn "Không thấy llama-server. Semantic search cần: brew install llama.cpp"
  warn " (cài sau cũng được — server tự spawn khi có, tier khác vẫn hoạt động)"
fi

# --- 5. Jan mcp_config.json ---------------------------------------------------
if (( NO_JAN )); then
  warn "Bỏ qua patch Jan (--no-jan)"
else
  JAN_CFG="$HOME/Library/Application Support/Jan/data/mcp_config.json"
  if [[ ! -d "$HOME/Library/Application Support/Jan" ]]; then
    warn "Không thấy Jan app — bỏ qua patch MCP config. Cài Jan rồi chạy lại install.sh."
  else
    say "Đăng ký MCP server 'pdf-ocr' vào Jan"
    .venv/bin/python patch_jan_config.py \
      --config "$JAN_CFG" \
      --server-cmd "$DIR/.venv/bin/python" \
      --server-script "$DIR/server.py" \
      --vault "$VAULT"
    say "Xong — RESTART Jan để nạp MCP server mới."
  fi
fi

# --- 6. watch folder (launchd) ----------------------------------------------
if (( NO_WATCH )); then
  warn "Bỏ qua launchd watch agent (--no-watch)"
else
  say "Cài launchd agent tự OCR khi thả file vào $VAULT/_dropbox/"
  PDF_OCR_VAULT="$VAULT" bash install_watch.sh
fi

echo
say "Hoàn tất. Kiểm tra nhanh:"
echo "  1) Thả file PDF/DOCX/ảnh vào: $VAULT/_dropbox/"
echo "     → ~1 phút sau note .md xuất hiện trong $VAULT/Inbox/"
echo "  2) Mở Jan → New Thread → hỏi: 'list files trong vault' (tool pdf-ocr)"
