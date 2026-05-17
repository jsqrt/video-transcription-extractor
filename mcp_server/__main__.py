from __future__ import annotations

# Apply network isolation BEFORE importing the MCP server so the loopback-only
# socket guard catches any accidental outbound calls from third-party stacks.
# Mirrors what app.__main__ does for the CLI.
from app.security.network_isolation import enforce_offline_mode

enforce_offline_mode()

from mcp_server.server import serve


if __name__ == "__main__":
    serve()
