# Google Workspace CLI - Remote MCP Server

This directory provides a **Remote Model Context Protocol (MCP) Server** wrapper for the `gws` (Google Workspace) CLI. It allows you to expose the full functionality of the `gws` CLI to remote MCP clients (such as Claude, custom agents, etc.) via a Streamable HTTP transport.

It is designed for **Enterprise Deployments** and includes OAuth 2.1 authorization integration, acting as an OAuth 2.1 Resource Server to securely validate clients.

> For a comprehensive enterprise deployment walkthrough (Kubernetes, Cloud Run, IdP integration, security hardening), see [docs/enterprise-deployment.md](../docs/enterprise-deployment.md).

## Architecture

1. **MCP Client** (e.g. an AI agent) obtains an OAuth 2.1 Bearer token from your Enterprise Identity Provider (IdP) (e.g., Okta, Auth0, Keycloak).
2. The Client connects to this Remote MCP Server via `/mcp`, passing the `Authorization: Bearer <token>` header.
3. The Server acts as a **Resource Server**, fetching the JWKS (JSON Web Key Set) from your IdP to cryptographically verify the token's signature, issuer, audience, and expiration.
4. Once authenticated, the Client can execute tools (e.g., `execute_gws`) which securely invoke the underlying `gws` CLI.

## Implementation

`python/` — built using `FastMCP` (from the official `mcp` SDK, `>=1.8.0` for `streamable_http_app()`) and `FastAPI`, mounted behind an ASGI OAuth 2.1 verification middleware.

## Enterprise Deployment Best Practices

### Prerequisites
- The `gws` CLI must be installed and authenticated on the host machine running the server. The server invokes `gws` as a subprocess. Use Service Accounts (`GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE`) for non-interactive enterprise use.
- An Enterprise IdP configured to issue JWTs to your clients.

### 1. Configuration (Environment Variables)

| Variable | Description | Default |
|---|---|---|
| `ENABLE_AUTH` | Must be set to exactly `true` or `false`. **The server refuses to start** if it is unset, empty, or any other value (e.g. `1`, `yes`) — see "Fail-closed startup" below. | *(required — no implicit default)* |
| `ALLOW_INSECURE_NO_AUTH` | Explicit opt-out for local testing: set to `true` to let the server start with auth disabled when `ENABLE_AUTH` is unset/ambiguous. Loudly logs a warning. Never use for a network-exposed deployment. | `false` |
| `OAUTH_ISSUER` | The URL of your Authorization Server (IdP). | `https://your-idp.example.com/` |
| `OAUTH_AUDIENCE` | The expected `aud` claim in the JWT. Usually the Canonical Server URI of this MCP server. | `https://mcp.example.com` |
| `JWKS_URI` | The URI to fetch public keys to verify JWT signatures. Defaults to `<OAUTH_ISSUER>/.well-known/jwks.json` (trailing-slash tolerant), which is correct for Auth0-style IdPs but **not** Okta — Okta serves JWKS at `<issuer>/v1/keys` instead, so set this explicitly for Okta deployments. | `$OAUTH_ISSUER/.well-known/jwks.json` |
| `REQUIRED_SCOPES` | Space-separated list: the JWT must contain at least one of these scopes. An explicit empty string with `ENABLE_AUTH=true` is treated as a misconfiguration (see `REQUIRE_SCOPES` below) rather than silently accepting any scope. | `gws:read gws:write` |
| `REQUIRE_SCOPES` | Set to `false` to explicitly opt out of scope enforcement when you intend `REQUIRED_SCOPES=""` to mean "no scope required." Without this, an empty `REQUIRED_SCOPES` with auth enabled refuses to start. | `true` |
| `GWS_ALLOWED_SERVICES` | Comma-separated list of `gws` service names clients may invoke. Set to `*` or omit to allow all. Note `auth`, `schema`, and `generate-skills` are CLI meta-commands and are never reachable through `execute_gws`, even under `*`; the `service:version` Discovery syntax is always rejected too (see §2). | *(unrestricted)* |
| `GWS_PER_USER_TOKEN` | Must be exactly `true` or `false` (like `ENABLE_AUTH`, an ambiguous value refuses to start rather than silently disabling isolation). Set to `true` to enable per-request credential isolation for multi-tenant deployments — each call executes as the calling user's own Google identity instead of the host's. Requires `ENABLE_AUTH=true`. See §3.5. | `false` |
| `GWS_USER_TOKEN_CLAIM` | When `GWS_PER_USER_TOKEN=true`, the name of a claim in the validated JWT payload to read the caller's Google token from. Takes priority over `GWS_USER_TOKEN_HEADER` if set. | *(unset — use header)* |
| `GWS_USER_TOKEN_HEADER` | When `GWS_PER_USER_TOKEN=true` and `GWS_USER_TOKEN_CLAIM` is unset, the forwarded HTTP header the client uses to carry its Google token. | `X-GWS-User-Token` |
| `EXPOSE_API_DOCS` | Set to `true` to mount and unauthenticate FastAPI's `/docs` (Swagger UI) and `/openapi.json`. Disabled by default — an unauthenticated schema/docs page is unnecessary information-disclosure surface on a network-exposed server. | `false` |
| `PORT` | The HTTP port to bind to. Only honored by the `python app.py` dev-mode entry point below — the `uvicorn`/`gunicorn` commands bind an explicit port and must be edited to match if you change it. | `8000` |

#### Fail-closed startup

`ENABLE_AUTH` has no implicit default — the server raises at import time (before it ever binds a port) if it's unset, empty, or anything other than exactly `"true"`/`"false"` (case-insensitive), *unless* `ALLOW_INSECURE_NO_AUTH=true` is also set. This closes the gap where a forgotten env var used to silently disable authentication for every path including `/mcp`. Similarly, an explicit empty `REQUIRED_SCOPES=""` combined with `ENABLE_AUTH=true` now refuses to start unless `REQUIRE_SCOPES=false` is set — it previously disabled the scope check silently.

```bash
# Refuses to start: ENABLE_AUTH unset
uvicorn app:app_with_auth --port 8000
# RuntimeError: ENABLE_AUTH=None is unset or ambiguous ...

# Refuses to start: "1" is not "true"/"false"
ENABLE_AUTH=1 uvicorn app:app_with_auth --port 8000

# Starts, auth on
ENABLE_AUTH=true OAUTH_ISSUER=... uvicorn app:app_with_auth --port 8000

# Starts, auth off — explicit and intentional (documented local-dev workflow)
ENABLE_AUTH=false uvicorn app:app_with_auth --port 8000

# Starts, auth off despite an unset/ambiguous ENABLE_AUTH — explicit, loud opt-out
ALLOW_INSECURE_NO_AUTH=true uvicorn app:app_with_auth --port 8000
```

### 2. Restricting which services MCP clients can call

Set `GWS_ALLOWED_SERVICES` to limit the `gws` services accessible through this server. Requests for any service not in the list are rejected **before** a subprocess is spawned, so there is no risk of the CLI accidentally executing a disallowed command.

`auth`, `schema`, and `generate-skills` are always rejected regardless of `GWS_ALLOWED_SERVICES`, including `*` — these are `gws` CLI meta-commands rather than Workspace services, and `auth` in particular can print decrypted OAuth credentials (`gws auth export --unmasked`) or destroy them (`gws auth logout`). The allowlist only governs which Workspace *services* (`drive`, `gmail`, ...) a client may reach.

```bash
# Only allow Drive, Gmail, and Calendar
GWS_ALLOWED_SERVICES=drive,gmail,calendar

# Broader deployment
GWS_ALLOWED_SERVICES=drive,gmail,calendar,sheets,docs,tasks

# Unrestricted — every service gws knows about, not just Workspace ones.
# gws resolves any Google Discovery API via "service:version" syntax for
# services it doesn't recognize by name (e.g. "compute:v1"); execute_gws
# rejects that syntax outright (see below), so even "*" here only reaches
# gws's own named service list, never an arbitrary Discovery API.
GWS_ALLOWED_SERVICES=*
```

**The allowlist check, in order, before any subprocess is spawned:**

1. `auth`/`schema`/`generate-skills` are always rejected (see above), regardless of `GWS_ALLOWED_SERVICES`.
2. A `service` containing `:` (e.g. `compute:v1`, `drive:v3`) is always rejected — this closes the Discovery `service:version` syntax as a way to reach APIs outside the allowlist's intent.
3. A `command` or `args` token of `--api-version` (or `--api-version=...`) is always rejected. `gws` applies this flag while keeping the *same* canonical service name (`crates/google-workspace-cli/src/main.rs`'s `parse_service_and_version` only overrides the Discovery *version*, not the `api_name`), so without this check a caller could keep an allowed service name (e.g. `reports`) while silently reaching a completely different Google API that happens to share that name (e.g. `admin`'s `directory_v1` full user/group management API instead of `reports_v1`'s read-only audit logs).
4. The service name is normalized to its canonical Discovery API name via the same alias table `gws` itself uses (`crates/google-workspace/src/services.rs`) — so `reports` and `admin-reports` are treated as the same service, whichever spelling you put in `GWS_ALLOWED_SERVICES` or the client passes as `service`.
5. The normalized name is checked against `GWS_ALLOWED_SERVICES`.

**Error response when a client requests a blocked service:**

```
Error: Service 'admin-reports' is not permitted on this server.
Allowed services: calendar, drive, gmail
```

**Error response when a client uses `service:version` syntax:**

```
Error: Service 'compute:v1' uses the 'api:version' Discovery syntax, which is
not permitted through execute_gws. ...
```

**Error response when a client uses `--api-version`:**

```
Error: '--api-version' is not permitted through execute_gws. It overrides the
Discovery API version while keeping the same service name, which can silently
repoint an allowed service at a different, unintended Google API.
```

### 3. Restricting which clients can connect — marking OAuth scopes as allowed

`REQUIRED_SCOPES` controls which *callers* may reach the `execute_gws` tool at all, independent of which services they're allowed to invoke once connected. This is enforced by `ASGIAuthMiddleware` in `python/app.py`, which reads the granted scopes out of the verified JWT and compares them against `REQUIRED_SCOPES`. Two claim formats are accepted — the standard space-delimited `scope` string, or an Okta-style `scp` array (Okta commonly issues the latter instead):

```python
token_scopes = _extract_token_scopes(payload)  # "scope" string, or "scp" array
if REQUIRED_SCOPES and not any(scope in token_scopes for scope in REQUIRED_SCOPES):
    ...  # 401 insufficient_scope
```

This is an **OR** match — the token needs *any one* of the scopes listed in `REQUIRED_SCOPES`, not all of them.

To mark a scope as "allowed", it has to exist on both sides:

1. **On your IdP**, define the scope(s) and grant them to the client application that will call this server:
   - **Okta**: Authorization Server → *Scopes* tab → add a custom scope (e.g. `gws:read`, `gws:write`), then include it in the client's requested scopes/consent.
   - **Auth0**: API → *Permissions* tab → add the scope, then assign it to the client's Application/API authorization (or include it in the `scope` parameter of the client's token request for machine-to-machine flows).
   - **Keycloak**: Client Scopes → create a client scope named after your scope string, then add it as a *Default* or *Optional* client scope on the client so it's included in issued access tokens.
   - The important part is that the resulting access token's `scope` claim (space-delimited string) contains the scope string you intend to require.
2. **On this server**, set `REQUIRED_SCOPES` to the same string(s), e.g.:

```bash
# Accept tokens carrying either gws:read or gws:write
REQUIRED_SCOPES="gws:read gws:write"

# Require a single, more specific scope
REQUIRED_SCOPES="gws:execute"
```

   Leaving `REQUIRED_SCOPES` unset falls back to the default `gws:read gws:write`. Setting it to an empty string is treated as a misconfiguration when `ENABLE_AUTH=true` — the server refuses to start unless you also set `REQUIRE_SCOPES=false`, an explicit acknowledgment that you intend "no scope required" rather than having forgotten to set scopes.

**Combining layers for defence-in-depth:**

1. **Google OAuth scopes** on the `gws` credentials — define the maximum Google Workspace permissions at the API level (e.g., `drive.readonly` credentials block all Drive write calls regardless of what the MCP server permits).
2. **`GWS_ALLOWED_SERVICES`** — restrict which services the MCP server will proxy.
3. **`REQUIRED_SCOPES`** on the IdP JWT — control which agents can connect to the server at all (see above).
4. **`GWS_PER_USER_TOKEN`** — for multi-tenant deployments, scope each call to the calling user's own Google identity instead of the host's (see §3.5).

### 3.5. Multi-tenant deployments: per-request credential isolation

By default, this server executes **every** request as the single Google identity configured on the host process (`GOOGLE_WORKSPACE_CLI_TOKEN` / `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE` / ADC). That's the right model for a single-tenant deployment, but with `stateless_http=True` one worker process serves many concurrent callers — so with only host credentials, every caller acts as the same Google account regardless of who authenticated. If you're deploying this server for multiple end users who should each act as themselves, set `GWS_PER_USER_TOKEN=true`.

```bash
GWS_PER_USER_TOKEN=true \
ENABLE_AUTH=true \
GWS_USER_TOKEN_HEADER=X-GWS-User-Token \
uvicorn app:app_with_auth --host 0.0.0.0 --port 8000
```

With this on, `ASGIAuthMiddleware` extracts a Google access token for the *calling user* on every request — after validating the caller's JWT — from one of two sources you choose:

- **`GWS_USER_TOKEN_CLAIM`** — a claim name in the already-validated JWT payload (e.g. if your IdP embeds a Google token via token exchange). Takes priority if set.
- **`GWS_USER_TOKEN_HEADER`** (default `X-GWS-User-Token`) — a forwarded HTTP header the client sets to its own Google access token.

That token is injected into the `gws` child process's environment (`GOOGLE_WORKSPACE_CLI_TOKEN`) for that one call only — built as a request-local dict, never written to `os.environ`, and never shared across concurrent requests (it's carried through a `contextvars.ContextVar`, not a module global, specifically because a plain global would leak across concurrent callers under `stateless_http`). If no token is available for a request, `execute_gws` returns an error rather than silently falling back to the host's credentials — the Rust CLI itself would otherwise fall through `GOOGLE_WORKSPACE_CLI_TOKEN=""` to `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE`/ADC, which is exactly the confused-deputy behavior this mode exists to prevent.

`GWS_PER_USER_TOKEN` must be exactly `true`/`false`/unset — like `ENABLE_AUTH`, an ambiguous value (e.g. `1`, `yes`) refuses to start rather than being silently treated as `false`, since that direction of ambiguity would quietly fall back to shared host-identity mode.

`GWS_PER_USER_TOKEN=true` requires `ENABLE_AUTH=true` — the server refuses to start otherwise, since per-user isolation is meaningless without a validated caller identity to isolate by. It also requires `server.py`'s `FastMCP(..., stateless_http=True)` to remain in effect: the isolation guarantee relies on each request running in a fresh task copied from that request's own context, so the `USER_TOKEN` ContextVar is never visible to another caller. The server checks this at startup too (`mcp.settings.stateless_http`) and refuses to start if it's ever turned off while per-user mode is on.

### 4. Running the Server

```bash
cd python
pip install -r requirements.txt

# Start the server
ENABLE_AUTH=true \
OAUTH_ISSUER="https://my-okta.com/oauth2/default" \
JWKS_URI="https://my-okta.com/oauth2/default/v1/keys" \
GWS_ALLOWED_SERVICES=drive,gmail,calendar \
uvicorn app:app_with_auth --host 0.0.0.0 --port 8000
```

For production, install `gunicorn` (not pinned in `requirements.txt`) and run it with `uvicorn` workers. Multiple `-w` workers are safe because `server.py` constructs `FastMCP(..., stateless_http=True)`, so no server-side session state is pinned to a particular worker:

```bash
pip install gunicorn
gunicorn app:app_with_auth -k uvicorn.workers.UvicornWorker -w 4 --bind 0.0.0.0:8000
```

### 5. Health Checks

For Kubernetes or Load Balancers (AWS ALB, etc.), the server exposes a public `/health` endpoint that bypasses OAuth verification:

```bash
curl -I http://localhost:8000/health
# HTTP/1.1 200 OK
```

The response body is `{"status": "ok", "service": "gws-mcp-server"}`.

`/docs` (Swagger UI) and `/openapi.json` are **not** mounted by default — an unauthenticated schema/docs page is unnecessary information-disclosure surface on a network-exposed server. Set `EXPOSE_API_DOCS=true` to restore them (e.g. for local development convenience); when enabled, they (and `/docs/oauth2-redirect`, needed for Swagger UI's "Authorize" flow) remain unauthenticated (see `ASGIAuthMiddleware` in `app.py`) and only expose the `/health` route's schema — the `/mcp` tool surface is mounted separately and is not reachable through them.

### 6. Logging and Observability

- Uses the standard `logging` module. Configure `logging.basicConfig` in `app.py` to emit JSON logs if your log aggregator (Datadog, Splunk, ELK) prefers structured logs. Note that `app.py` calls `logging.basicConfig()` **before** importing `server.py` — importing first would let `server.py`'s own logging calls implicitly configure the root logger at the default `WARNING` level, silently swallowing the `INFO` logs this server relies on for auditing.
- Each `gws` CLI invocation logs only the `service` and the first two whitespace-separated tokens of `command` (e.g. `service='gmail' command='messages send'`) — never the full `command` string, `--params`, or `args`, which can carry request PII (message bodies, search queries, recipient addresses, resource IDs). The two-token cap matters because every Discovery-driven `gws` method takes its parameters via `--params`/`--json` rather than positionally, so a legitimate `command` never needs more than a resource+verb pair — anything a caller appends beyond that is either noise or exactly the kind of identifier this redaction exists to keep out of the logs. The Google credential itself is passed via the child process's environment, not the command line, so it never appears in logs either way.
- Rejected/invalid-token responses never echo the underlying exception text to the client (e.g. a JWKS fetch failure's connection error, which can embed the configured JWKS URI) — the detail is logged server-side only, and the client gets a generic `"Invalid token"` message.

### 7. Client Connection

MCP Clients must connect using the **Streamable HTTP Transport** (supported by `@modelcontextprotocol/sdk` >= 1.4, or any spec-compliant MCP client).

- **Streamable Endpoint**: `/mcp` — the FastMCP sub-app handles all the methods the transport needs (`POST` for requests, `GET` for the SSE stream, `DELETE` to terminate a session) at this single path.

Clients **must** include the header:
```
Authorization: Bearer <YOUR_ACCESS_TOKEN>
```
If the token is invalid or missing, the server returns `401 Unauthorized` with a `WWW-Authenticate` header conforming to [RFC 6750](https://datatracker.ietf.org/doc/html/rfc6750) and the MCP Authorization Spec.

### 8. Testing

```bash
cd python
pip install -r requirements-dev.txt
pytest
```

Covers the service allowlist (`:` rejection, `--api-version` rejection, alias normalization, always-blocked meta-commands), per-request token isolation (including a concurrency test asserting two simultaneous calls never observe each other's token, and the `GWS_PER_USER_TOKEN`/`ENABLE_AUTH`/`stateless_http` startup cross-checks), the `ENABLE_AUTH`/`REQUIRED_SCOPES` fail-closed startup guards, the logging redaction, and the Okta `scp`-claim and exception-text-redaction fixes. This suite runs in CI (`.github/workflows/ci.yml`) on any change under `remote-mcp-server/python/`.
