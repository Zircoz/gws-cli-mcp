# AGENTS.md — remote-mcp-server

Scope: this file governs `remote-mcp-server/` only. The repository root [`AGENTS.md`](../AGENTS.md) covers the Rust `gws` CLI itself (build/test, Discovery-driven architecture, changesets, PR labels) and still applies to this directory for anything it doesn't override below.

## Project Overview

This directory contains a Python reference implementation of an **enterprise-facing Remote MCP Server** that wraps the `gws` CLI as a subprocess and exposes it over Streamable HTTP, protected by OAuth 2.1 Bearer token validation:

| Path | Stack | Entry point |
|---|---|---|
| `python/` | FastAPI + FastMCP (`mcp` SDK, `>=1.8.0`) | `app.py` (mounts `server.py`'s `FastMCP` instance at `/`, which itself serves `/mcp`) |

> [!NOTE]
> A Node.js (Express) implementation previously lived in `nodejs/` alongside this one. It was removed because maintaining two parallel, hand-written implementations of the same auth/allowlist/subprocess logic let them drift out of behavioral parity (e.g. one enforced `REQUIRED_SCOPES`, the other silently didn't) without either side's tests catching it — there were no tests. Don't re-add a second-language implementation here unless there's a concrete need driving it; prefer keeping one well-tested implementation over two unverified ones.

> [!IMPORTANT]
> Do not add new "generated" Google API crates or SDKs here — this directory only ever *invokes the `gws` binary as a subprocess* (`asyncio.create_subprocess_exec`). It must never call Google APIs directly; that's `gws`'s job. This keeps the auth model, retry logic, and Discovery-driven command surface in one place (the Rust CLI).

## Local Development

```bash
cd remote-mcp-server/python
pip install -r requirements.txt
ENABLE_AUTH=false uvicorn app:app_with_auth --reload --port 8000
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

The second command is the actual regression test for the FastMCP mount/lifespan wiring in `app.py` — a broken mount path or a missing `mcp.session_manager.run()` in the lifespan will make this hang or 500 rather than return a `tools/list` result. Always run it after touching `app.py`'s mount, lifespan, or middleware setup.

When adding non-trivial logic (allowlist parsing, auth middleware, error formatting), prefer extracting it into a plain function that can be unit-tested (`pytest`) rather than inlining it in the request handler — matches the root AGENTS.md's testability guidance for the Rust side.

## Security-Sensitive Areas — Change These Carefully

1. **`ALLOWED_SERVICES` / `GWS_ALLOWED_SERVICES`** (`python/server.py`) — must be checked *before* the `gws` subprocess is spawned, never after. This is the primary defense against a client invoking an unintended service.
2. **`ALWAYS_BLOCKED_SERVICES`** (`python/server.py`) — `auth`, `schema`, and `generate-skills` are hardcoded as unreachable through `execute_gws` regardless of `GWS_ALLOWED_SERVICES`, including `*`. These are `gws` CLI meta-commands, not Workspace services; `gws auth export --unmasked` prints decrypted OAuth credentials and `gws auth logout` destroys them. Do not let this list be satisfied by `GWS_ALLOWED_SERVICES` — it must be checked independently and first, as it is now.
3. **Auth bypass path list** — `ASGIAuthMiddleware` in `app.py` bypasses verification for `/health`, `/docs`, `/openapi.json`. If you add new unauthenticated routes, update both the middleware allowlist *and* the README's Health Checks section in the same change.
4. **Logging import order** — `app.py` calls `logging.basicConfig(level=logging.INFO)` *before* `from server import mcp`. `server.py` logs two lines at import time (its allowlist summary) via a module-level `logger = logging.getLogger(__name__)`; those lines only reach `INFO` if `basicConfig()` has already run. Don't reorder the import above the `basicConfig()` call.
5. **Logging content** — never log `--params` contents or full JWTs. The server currently logs the constructed `gws` command line, which can contain user-supplied arguments. Redact before adding structured logging.
6. **`--params` / `args` forwarding** — these are passed through to `gws` largely as-is (JSON-encoded for `--params`, raw strings for `args`). Do not add shell interpolation (`shell=True`) — the implementation correctly uses `create_subprocess_exec`'s argument-array form, which avoids shell injection. Keep it that way.
7. **FastMCP mount path** — `mcp.streamable_http_app()` already serves at its own `streamable_http_path` (default `/mcp`). `app.py` mounts it at `app.mount("/", mcp_starlette)`, not `"/mcp"` — mounting it under `/mcp` again would nest the real endpoint at `/mcp/mcp`. Verify with the `tools/list` curl command above if you touch this.
8. **Session manager lifespan** — the streamable HTTP transport's session manager must be entered via `async with mcp.session_manager.run():` inside `app.py`'s `lifespan()`. Mounting the sub-app alone does not start it; a mounted sub-app's own lifespan never runs.
9. **`stateless_http=True`** (`python/server.py`'s `FastMCP(...)` constructor) — without this, MCP session state lives in an in-memory dict on whichever worker process handled `initialize`; a multi-worker deployment (`gunicorn -w N`, which every doc here recommends) would 404 follow-up requests routed to a different worker. Don't remove this flag without also removing the multi-worker guidance in README.md and docs/enterprise-deployment.md, or replacing it with a shared session store.
10. **JWT verification hardening** (`app.py`'s `ASGIAuthMiddleware`) — `jwt.decode(...)` passes `options={"require": ["exp"]}` so tokens without an expiry are rejected rather than accepted forever; and `client.get_signing_key_from_jwt(...)` runs via `anyio.to_thread.run_sync` because `PyJWKClient` does a blocking HTTP fetch on a JWKS cache miss, which would otherwise stall the whole async event loop (including other clients' in-flight SSE streams) on a slow/unreachable IdP. Keep both when touching this code path.

## Keeping README.md in Sync

`README.md` in this directory documents every environment variable, auth behavior, and endpoint the server exposes. When you change:
- an env var's name, default, or semantics → update the table in README §1
- allowlist/scope-check logic → update README §2/§3
- an unauthenticated route → update README §5
- the `/mcp` method surface → update README §7

Treat a code change that isn't reflected in the README as incomplete.

## Changesets

Even though this directory is Python (not Rust), it still ships as part of the `@googleworkspace/cli` package release for versioning purposes. Follow the root `AGENTS.md` changeset instructions — add a `.changeset/<descriptive-name>.md` file for any user-visible change here (`minor` for new server features/env vars, `patch` for docs/fixes).
