# Repository Guidelines

## Project Structure & Module Organization

- `src/kernel/nachuan/` contains the Python host: routing, accounts, actions, quotas, persistence, and the HTTP API. Tests and local fixtures live in `src/kernel/tests/`.
- `src/plugin-runtime/src/` contains the TypeScript subprocess host and shared plugin contracts. Platform adapters live in `src/plugins/2api-plugin-*/`, each with a `spec.json` and `index.ts`.
- `src/console/` contains the React/Vite console and its tests. `src/contracts/schema-fixtures.json` supplies shared Python/TypeScript validation cases.
- `scripts/` contains setup, test, and launch scripts; `docs/` and `examples/` contain design and configuration references. The sibling workspace directory `04_核心源码/` mirrors applicable source files; synchronize it when changing mirrored code.

## Build, Test, and Development Commands

Run commands from this directory. Node 24.18.0 or later and pnpm 12 or later are required.

- `bash scripts/setup.sh`: install dependencies, build the plugin runtime, and create local configuration if absent.
- `bash scripts/test.sh`: build the runtime, run Python and TypeScript tests, test the console, and build the console.
- `bash scripts/run-kernel.sh`: start the kernel at `http://127.0.0.1:7865` using `src/backend/config.local.json`.
- `bash scripts/run-console.sh`: start the Vite console at `http://127.0.0.1:5173`.
- `(cd src/kernel && python3 -m unittest discover -s tests -v)`: run kernel tests after building the runtime.

## Coding Style & Architecture

Use four-space indentation in Python and two spaces in TypeScript, following nearby code. Name Python modules and tests with `snake_case`; use `*.test.ts` for runtime tests and `*.test.cjs` for console tests. Keep the Python kernel platform-independent; place provider authentication and protocol translation in TypeScript plugins. The runtime and console builds run TypeScript checks; no separate formatter or linter is configured.

## Testing Guidelines

Use Python `unittest` and Node's built-in test runner. Add focused regression tests for behavior changes, including failure and cancellation paths where relevant. Update shared schema fixtures when changing validation semantics. Integration tests use local fake upstreams; passing them does not establish real-provider compatibility.

## Commits, Pull Requests & Configuration

Git history contains only an initial handoff commit, so no established commit convention can be inferred. Use concise, imperative subjects. Pull requests should explain behavior changes and validation, link relevant issues when available, and include screenshots for visible console changes. Never commit credentials, `config.local.json`, runtime data, or generated dependencies. Preserve existing uncommitted work.
