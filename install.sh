#!/usr/bin/env bash
# DeepSeek Usage for Hermes Desktop — one-command installer.
#   curl -fsSL https://raw.githubusercontent.com/JicoHe/hermes-deepseek-usage/main/install.sh | bash
# or:  ./install.sh [owner/repo]
set -euo pipefail

SOURCE="${1:-JicoHe/hermes-deepseek-usage}"

if ! command -v hermes >/dev/null 2>&1; then
  echo "找不到 hermes 命令。先安装 Hermes Agent：https://hermes-agent.nousresearch.com" >&2
  exit 1
fi

echo "→ 安装 $SOURCE"
hermes plugins install "$SOURCE" --enable

cat <<'EOF'

装好了，还差两步：

  1) 让后端生效：        hermes gateway restart
  2) 打开桌面半区：      Hermes → 设置 → 能力 → 插件 → DeepSeek Usage

API key（二选一）：
  · 写进 ~/.hermes/.env ：DEEPSEEK_API_KEY=sk-...
  · 或者沿用 ~/.codex/config.toml 里的 experimental_bearer_token
EOF
