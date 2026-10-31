#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== Python kernel =="
(
  cd src/kernel
  python3 -m unittest discover -s tests -v
)

echo "== TypeScript plugin runtime =="
pnpm --dir src/plugin-runtime build
pnpm --dir src/plugin-runtime test

echo "== Console production build =="
pnpm --dir src/console build
