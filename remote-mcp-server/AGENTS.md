# AGENTS.md — remote-mcp-server

Scope: this file governs `remote-mcp-server/` only. The repository root [`AGENTS.md`](../AGENTS.md) covers the Rust `gws` CLI itself (build/test, Discovery-driven architecture, changesets, PR labels) and still applies to this directory for anything it doesn't override below.

## Project Overview

This directory contains two reference implementations of an **enterprise-facing Remote MCP Server** that wraps the `gws` CLI as a subprocess and exposes it over Streamable HTTP, protected by OAuth 2.1 Bearer token validation:

| Path | Stack | Entry point |
|---|---|---|
| `python/` | FastAPI + FastMCP (`mcp` SDK) | `app.py` (mounts `server.py`'s `FastMCP` instance at `/mcp`) |
| `nodejs/` | Express + `@modelcontextprotocol/sdk` | `server.js` |

Both implementations must stay behaviorally equivalent for the features they both claim to support (see `README.md`'s env var table). If a feature is intentionally implementation-specific (e.g. `REQUIRED_SCOPES` is currently Python-only), call that out explicitly in the README rather than letting the docs silently drift from the code — README accuracy for this directory is treated as part of the change, not an afterthought.

> [!IMPORTANT]
> Do not add new "generated" Google API crates or SDKs here — this directory only ever *invokes the `gws` binary as a subprocess* (`asyncio.create_subprocess_exec` in Python, `child_process.spawn` in Node). It must never call Google APIs directly; that's `gws`'s job. This keeps the auth model, retry logic, and Discovery-driven command surface in one place (the Rust CLI).

## Local Development

**Python:**
```bash
cd remote-mcp-server/python
pip install -r requirements.txt
ENABLE_AUTH=false uvicorn app:app_with_auth --reload --port 8000
```

**Node.js:**
```bash
cd remote-mcp-server/nodejs
npm install   # or: pnpm install — see root AGENTS.md's package-manager note
ENABLE_AUTH=false node server.js
```

Leave `ENABLE_AUTH=false` (the default) for local iteration so you don't need a live IdP; `gws` itself still needs valid credentials on `PATH` (see `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE` in the root `AGENTS.md`'s Environment Variables section).

## Testing

There is currently no automated test suite in this directory. Until one exists, verify changes manually:

```bash
curl -i http://localhost:8000/health

curl -i http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

When adding non-trivial logic (allowlist parsing, auth middleware, error formatting), prefer extracting it into a plain function that can be unit-tested (`pytest` for Python, `node --test` or `vitest` for Node) rather than inlining it in the request handler — matches the root AGENTS.md's testability guidance for the Rust side.

## Security-Sensitive Areas — Change These Carefully

1. **`ALLOWED_SERVICES` / `GWS_ALLOWED_SERVICES`** (`python/server.py`, `nodejs/server.js`) — must be checked *before* the `gws` subprocess is spawned, never after. This is the primary defense against a client invoking an unintended service.
2. **Auth bypass path lists** — Python's `ASGIAuthMiddleware` bypasses verification for `/health`, `/docs`, `/openapi.json`; Node's `authMiddleware` bypasses only `/health`. If you add new unauthenticated routes, update both the middleware allowlist *and* the README's Health Checks section in the same change.
3. **Scope enforcement (`REQUIRED_SCOPES`)** — only implemented in Python today (`app.py`). If you port it to Node, remove the parity warning in `README.md` §3 and update the env var table's "Python only" annotation.
4. **Logging** — never log `--params` contents or full JWTs; both implementations currently log the constructed `gws` command line, which can contain user-supplied arguments. Redact before adding structured logging.
5. **`--params` / `args` forwarding** — these are passed through to `gws` largely as-is (JSON-encoded for `--params`, raw strings for `args`). Do not add shell interpolation (e.g. `shell: true` in Node, `shell=True` in Python) — both implementations correctly use `exec`/`spawn` argument-array forms, which avoids shell injection. Keep it that way.

## Keeping README.md in Sync

`README.md` in this directory documents every environment variable, auth behavior, and endpoint the two servers expose. When you change:
- an env var's name, default, or semantics → update the table in README §1
- allowlist/scope-check logic → update README §2/§3
- an unauthenticated route → update README §6
- the `/mcp` method surface → update README §8

Treat a code change that isn't reflected in the README as incomplete.

## Changesets

Even though this directory is Python/Node (not Rust), it still ships as part of the `@googleworkspace/cli` package release for versioning purposes. Follow the root `AGENTS.md` changeset instructions — add a `.changeset/<descriptive-name>.md` file for any user-visible change here (`minor` for new server features/env vars, `patch` for docs/fixes).
