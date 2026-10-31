#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

command -v python3 >/dev/null || { echo "缺少 python3"; exit 1; }
command -v node >/dev/null || { echo "缺少 node"; exit 1; }
command -v pnpm >/dev/null || { echo "缺少 pnpm；建议先启用 corepack 或安装 pnpm 12"; exit 1; }

if [ ! -f src/backend/config.local.json ]; then
  cp examples/config.example.json src/backend/config.local.json
  echo "已生成 src/backend/config.local.json；运行前请修改 api_key。"
fi

python3 -m pip install -r requirements.txt
pnpm --dir src/plugin-runtime install --frozen-lockfile
pnpm --dir src/plugin-runtime build
pnpm --dir src/console install --frozen-lockfile

echo "安装完成。下一步: pnpm test"
