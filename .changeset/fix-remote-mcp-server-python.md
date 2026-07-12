---
"@googleworkspace/cli": patch
---

Fix the Python remote MCP server, which was non-functional: the streamable HTTP session manager was never started (every `/mcp` request 500'd), the transport was double-mounted at `/mcp/mcp` instead of `/mcp`, `requirements.txt` pinned an `mcp` version too old to support `streamable_http_app()`, and import ordering silently suppressed all `INFO`-level audit logging. Also removes the Node.js reference implementation (`remote-mcp-server/nodejs/`), which had the same broken `/mcp` wiring plus missing `REQUIRED_SCOPES` enforcement, in favor of maintaining a single, tested implementation.
