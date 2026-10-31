#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/src/kernel"

if [ ! -f "$ROOT/src/backend/config.local.json" ]; then
  echo "缺少 src/backend/config.local.json；请先运行 scripts/setup.sh"
  exit 1
fi

exec python3 -m nachuan.app \
  --config "$ROOT/src/backend/config.local.json" \
  --runtime "$ROOT/src/plugin-runtime" \
  --host 127.0.0.1 \
  --port 7865
