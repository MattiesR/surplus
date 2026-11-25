#!/bin/sh
# Isolated backend for the e2e smoke tests: temp data/config dirs, no real logs, and a FakeClaude CLI
# (recorded fixture), so the tests never read real data or spend quota.
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
E2E_DIR="${E2E_DIR:-/tmp/surplus-e2e}"
rm -rf "$E2E_DIR" && mkdir -p "$E2E_DIR/claude/projects"
cat > "$E2E_DIR/claude-fake" <<SH
#!/bin/sh
exec python3 "$ROOT/backend/tests/fixtures/fake_claude.py" "\$@"
SH
chmod +x "$E2E_DIR/claude-fake"
export SURPLUS_DATA_DIR="$E2E_DIR/data" SURPLUS_CONFIG_DIR="$E2E_DIR/config" SURPLUS_CLAUDE_DIR="$E2E_DIR/claude"
export SURPLUS_CLAUDE_BIN="$E2E_DIR/claude-fake" SURPLUS_PROJECTS_ROOT="$E2E_DIR/projects"
cd "$ROOT" && exec uv run surplus serve --port 8799
