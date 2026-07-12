---
"@googleworkspace/cli": minor
---

Add a Python (FastAPI/FastMCP) Remote MCP Server reference implementation under `remote-mcp-server/`, exposing `gws` over Streamable HTTP with OAuth 2.1 Bearer token validation, a `GWS_ALLOWED_SERVICES` allowlist (with `auth`/`schema`/`generate-skills` always blocked), and an enterprise deployment guide (`docs/enterprise-deployment.md`) covering Kubernetes, Cloud Run, and IdP integration.
