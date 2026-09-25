#!/bin/bash

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PINAK_HOME="${PINAK_MCP_HOME:-${HOME}/pinak-memory}"
TARGET_DIR="${PINAK_HOME}/bin"
SCHEMA_DIR="${PINAK_HOME}/schemas"
TEMPLATE_DIR="${PINAK_HOME}/templates"

PYTHON_BIN="${PINAK_MCP_PYTHON:-python3}"

/bin/mkdir -p "$TARGET_DIR"
/bin/mkdir -p "$SCHEMA_DIR"
/bin/mkdir -p "$TEMPLATE_DIR"

/bin/cp "$BASE_DIR/client/pinak_memory_mcp.py" "$TARGET_DIR/pinak_memory_mcp.py"
/bin/cp -R "$BASE_DIR/schemas/." "$SCHEMA_DIR/"
/bin/cp -R "$BASE_DIR/templates/." "$TEMPLATE_DIR/"

/bin/cat > "$TARGET_DIR/pinak-mcp" <<'EOF'
#!/bin/bash
set -euo pipefail

PYTHON_BIN="${PINAK_MCP_PYTHON:-python3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec "$PYTHON_BIN" "$SCRIPT_DIR/pinak_memory_mcp.py"
EOF

/bin/chmod 755 "$TARGET_DIR/pinak-mcp"
/bin/chmod 644 "$TARGET_DIR/pinak_memory_mcp.py"

/bin/echo "Installed MCP client to $TARGET_DIR"
/bin/echo "Schemas synced to $SCHEMA_DIR"
/bin/echo "Templates synced to $TEMPLATE_DIR"
/bin/echo "Desktop client config was not changed. Use scripts/pinak_mcp_config.py install/verify with an explicit client; verify runtime and authentication separately."
