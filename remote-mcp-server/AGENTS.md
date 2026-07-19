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

`ENABLE_AUTH` must now be set explicitly to exactly `true` or `false` — `app.py` refuses to start otherwise (see "ENABLE_AUTH fail-closed startup" below). Explicit `ENABLE_AUTH=false`, as shown above, remains the documented way to iterate locally without a live IdP; `gws` itself still needs valid credentials on `PATH` (see `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE` in the root `AGENTS.md`'s Environment Variables section).

## Testing

```bash
cd remote-mcp-server/python
pip install -r requirements-dev.txt
pytest
```

`tests/test_server.py` and `tests/test_app.py` cover the security-sensitive logic: the service allowlist (`:` rejection, `--api-version` rejection, alias normalization, `ALWAYS_BLOCKED_SERVICES`), per-request token isolation and its concurrent-request/empty-token fail-closed behavior, the `ENABLE_AUTH`/`REQUIRED_SCOPES`/`GWS_PER_USER_TOKEN`/`stateless_http` startup guards, the logging redaction (both the command-truncation and the exception-text-not-leaked-to-client cases), and the Okta `scp`-claim scope check. `app.py`/`server.py` perform their startup validation at import time, so tests exercise it through extracted helpers (`_resolve_enable_auth`, `_resolve_required_scopes`, `_resolve_per_user_token_mode`, `_check_per_user_token_preconditions`, `_build_unauthenticated_paths`, `_extract_token_scopes`) rather than re-importing the module per case. `tests/conftest.py` *forces* (not merely defaults) `ENABLE_AUTH=true` and clears `REQUIRED_SCOPES`/`GWS_PER_USER_TOKEN` before any test module imports `app`/`server` — using `os.environ.setdefault()` here would be a no-op against an ambient shell/CI environment that already exports a conflicting value, which would fail the entire suite at collection instead of just running with clean state. This suite runs in CI on any change under `remote-mcp-server/python/` (see the `remote-mcp-python-test` job in `.github/workflows/ci.yml`).

Also manually verify the FastMCP mount/lifespan wiring after touching it — no unit test exercises the ASGI transport itself:

```bash
curl -i http://localhost:8000/health

curl -i http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

The second command is the actual regression check for the FastMCP mount/lifespan wiring in `app.py` — a broken mount path or a missing `mcp.session_manager.run()` in the lifespan will make this hang or 500 rather than return a `tools/list` result. Always run it after touching `app.py`'s mount, lifespan, or middleware setup.

When adding non-trivial logic (allowlist parsing, auth middleware, error formatting), prefer extracting it into a plain function that can be unit-tested (`pytest`) rather than inlining it in the request handler — matches the root AGENTS.md's testability guidance for the Rust side.

## Security-Sensitive Areas — Change These Carefully

1. **`ALLOWED_SERVICES` / `GWS_ALLOWED_SERVICES`** (`python/server.py`) — must be checked *before* the `gws` subprocess is spawned, never after. This is the primary defense against a client invoking an unintended service. The check has four parts, in order: reject `service` strings containing `:` (closes the `service:version` Discovery escape hatch — see `crates/google-workspace/src/discovery.rs`); reject any `command`/`args` token of `--api-version`/`--api-version=...` (see item 14 below); normalize aliases to their canonical Discovery API name via `SERVICE_ALIASES` (mirrored from `crates/google-workspace/src/services.rs`'s `SERVICES` table — keep the two in sync); then compare the canonicalized name against `ALLOWED_SERVICES_CANONICAL`. Without alias normalization, allowing `reports` would not also allow the equivalent `admin-reports`, and vice versa.
2. **`ALWAYS_BLOCKED_SERVICES`** (`python/server.py`) — `auth`, `schema`, and `generate-skills` are hardcoded as unreachable through `execute_gws` regardless of `GWS_ALLOWED_SERVICES`, including `*`. These are `gws` CLI meta-commands, not Workspace services; `gws auth export --unmasked` prints decrypted OAuth credentials and `gws auth logout` destroys them. Do not let this list be satisfied by `GWS_ALLOWED_SERVICES` — it must be checked independently and first, as it is now.
3. **Auth bypass path list** — `ASGIAuthMiddleware` in `app.py` bypasses verification for `/health` always, and for `/docs`/`/openapi.json`/`/docs/oauth2-redirect` only when `EXPOSE_API_DOCS=true` (default `false` — the docs/openapi routes aren't even mounted otherwise, since `FastAPI(docs_url=..., openapi_url=...)` is `None` unless opted in; `/docs/oauth2-redirect` is FastAPI's default Swagger OAuth2 redirect path and needs the same bypass or the docs UI's "Authorize" flow 401s). Built by `_build_unauthenticated_paths()`. If you add new unauthenticated routes, update that function *and* the README's Health Checks section in the same change.
4. **Logging import order** — `app.py` calls `logging.basicConfig(level=logging.INFO)` *before* `from server import mcp`. `server.py` logs two lines at import time (its allowlist summary) via a module-level `logger = logging.getLogger(__name__)`; those lines only reach `INFO` if `basicConfig()` has already run. Don't reorder the import above the `basicConfig()` call.
5. **Logging content** — never log `--params` or `args` contents, full JWTs, or raw exception text from token validation failures. `execute_gws` logs only `service` and the first two whitespace-split tokens of `command` (e.g. `service='gmail' command='messages send'`) — never the full `command` string, `--params` JSON, or raw `args`, any of which can carry request PII (message bodies, search queries, resource IDs). The two-token cap exists because every Discovery-driven `gws` method takes its parameters via `--params`/`--json`, never positionally, so a legitimate `command` is never more than a resource+verb pair. Separately, `ASGIAuthMiddleware`'s `except Exception` handler logs the exception server-side (`logger.warning`) but returns only a generic `"Invalid token"` to the client — `str(e)` can embed internals like the configured JWKS URI (e.g. `PyJWKClient` connection errors). Keep new logging/error-response code to these same shapes.
6. **`--params` / `args` forwarding** — these are passed through to `gws` largely as-is (JSON-encoded for `--params`, raw strings for `args`). Do not add shell interpolation (`shell=True`) — the implementation correctly uses `create_subprocess_exec`'s argument-array form, which avoids shell injection. Keep it that way.
7. **FastMCP mount path** — `mcp.streamable_http_app()` already serves at its own `streamable_http_path` (default `/mcp`). `app.py` mounts it at `app.mount("/", mcp_starlette)`, not `"/mcp"` — mounting it under `/mcp` again would nest the real endpoint at `/mcp/mcp`. Verify with the `tools/list` curl command above if you touch this.
8. **Session manager lifespan** — the streamable HTTP transport's session manager must be entered via `async with mcp.session_manager.run():` inside `app.py`'s `lifespan()`. Mounting the sub-app alone does not start it; a mounted sub-app's own lifespan never runs.
9. **`stateless_http=True`** (`python/server.py`'s `FastMCP(...)` constructor) — without this, MCP session state lives in an in-memory dict on whichever worker process handled `initialize`; a multi-worker deployment (`gunicorn -w N`, which every doc here recommends) would 404 follow-up requests routed to a different worker. Don't remove this flag without also removing the multi-worker guidance in README.md and docs/enterprise-deployment.md, or replacing it with a shared session store. The same shared-worker model is why per-request state (like the per-user token below) must never be a plain module global. **This flag is also load-bearing for `GWS_PER_USER_TOKEN`'s correctness** (see item 13): `app.py` checks `mcp.settings.stateless_http` at startup and refuses to start with per-user mode on if it's ever `False`. Do not remove that check when touching this flag.
10. **JWT verification hardening** (`app.py`'s `ASGIAuthMiddleware`) — `jwt.decode(...)` passes `options={"require": ["exp"]}` so tokens without an expiry are rejected rather than accepted forever; and `client.get_signing_key_from_jwt(...)` runs via `anyio.to_thread.run_sync` because `PyJWKClient` does a blocking HTTP fetch on a JWKS cache miss, which would otherwise stall the whole async event loop (including other clients' in-flight SSE streams) on a slow/unreachable IdP. Keep both when touching this code path.
11. **`ENABLE_AUTH` fail-closed startup** (`app.py`'s `_resolve_enable_auth`) — `ENABLE_AUTH` must be exactly `"true"` or `"false"` (case-insensitive); anything else (unset, empty, `"1"`, `"yes"`, typos) raises `RuntimeError` at import time unless `ALLOW_INSECURE_NO_AUTH=true` is explicitly set (which loudly logs a warning and disables auth). A forgotten env var must never silently disable authentication on a network-exposed server. Do not widen the accepted "true"/"false" set or make this check advisory.
12. **`REQUIRED_SCOPES` misconfiguration guard** (`app.py`'s `_resolve_required_scopes`) — an explicit empty `REQUIRED_SCOPES=""` with `ENABLE_AUTH=true` raises at import time unless `REQUIRE_SCOPES=false` is explicitly set; it used to silently disable the scope check (any validly-signed token would connect). Leaving `REQUIRED_SCOPES` unset is unaffected — it still falls back to the documented default.
13. **Per-request credential isolation** (`GWS_PER_USER_TOKEN`, `server.py` + `app.py`) — this is the fix for the single-tenant confused-deputy problem: by default every request executes as the host's Google identity (`GOOGLE_WORKSPACE_CLI_TOKEN`/credentials file/ADC in the process environment), and `stateless_http=True` means one worker serves many concurrent callers. `GWS_PER_USER_TOKEN` is parsed strictly by `server.py`'s `_resolve_per_user_token_mode()` (exactly `"true"`/`"false"`/unset; ambiguous values raise `RuntimeError` at import time) — unlike the other new boolean env vars here, an ambiguous value for this one would fail *toward less security* (silently falling back to shared host-identity mode), so it does not get the lenient `.lower() == "true"` treatment. When `GWS_PER_USER_TOKEN=true`, `ASGIAuthMiddleware` extracts the caller's own Google token (from the `GWS_USER_TOKEN_CLAIM` JWT claim if set, else the `GWS_USER_TOKEN_HEADER` header) and stores it in `server.USER_TOKEN`, a `contextvars.ContextVar` — **not** a module-level dict/global, which would leak one caller's token into another's concurrent request under `stateless_http`. `execute_gws` builds the child's `env=` explicitly per call and injects the token only into that one subprocess; it never mutates `os.environ` (which would race across concurrent requests) and never falls through to host credentials when the token is empty — it errors instead. `app.py`'s `_check_per_user_token_preconditions()` refuses to start with `GWS_PER_USER_TOKEN=true` unless both `ENABLE_AUTH=true` (per-user isolation is meaningless without a validated caller identity) and `mcp.settings.stateless_http` is `True` (see item 9 — the ContextVar propagation guarantee depends on it). If `GWS_USER_TOKEN_CLAIM`/`GWS_USER_TOKEN_HEADER` are set while `GWS_PER_USER_TOKEN` is off, `app.py` logs a warning (it's a no-op, not an error, since the server still runs — just in the shared host-identity mode the operator may not have intended). Keep all of these invariants together when touching this code path.
14. **`--api-version` escape hatch** (`python/server.py`'s `execute_gws`) — `gws`'s `--api-version` flag (`crates/google-workspace-cli/src/main.rs`'s `parse_service_and_version`) overrides only the Discovery *version* while keeping the `service` argument's canonical `api_name`, so it can silently repoint an allowlisted service (e.g. `reports`) at an entirely different Google API that happens to share that `api_name` (e.g. `admin`'s `directory_v1` full user/group management API, instead of `reports_v1`'s read-only audit logs) — without the `service` argument the allowlist checks ever changing. `execute_gws` rejects any `command`/`args` token equal to `--api-version` or starting with `--api-version=` before the allowlist check runs. If `gws` ever grows another flag that can change which Discovery document gets fetched, it needs the same treatment here.
15. **Okta `scp` scope claim support** (`app.py`'s `_extract_token_scopes()`) — Okta (an IdP this server's docs explicitly support) commonly issues scopes as an `scp` array claim rather than the standard space-delimited `scope` string; `_extract_token_scopes()` accepts either, preferring `scope` if both are present. Keep both paths when touching the scope check.

## Keeping README.md in Sync

`README.md` in this directory documents every environment variable, auth behavior, and endpoint the server exposes. When you change:
- an env var's name, default, or semantics → update the table in README §1
- allowlist/scope-check logic → update README §2/§3
- per-request credential isolation (`GWS_PER_USER_TOKEN`) → update README §3.5
- an unauthenticated route → update README §5
- the `/mcp` method surface → update README §7
- test coverage → update README §8 and this file's Testing section

Treat a code change that isn't reflected in the README as incomplete.

## Changesets

Even though this directory is Python (not Rust), it still ships as part of the `@googleworkspace/cli` package release for versioning purposes. Follow the root `AGENTS.md` changeset instructions — add a `.changeset/<descriptive-name>.md` file for any user-visible change here (`minor` for new server features/env vars, `patch` for docs/fixes).
