# Plan: OAuth2 Remote MCP Server for `gws`

## Context

`gws` is a dynamic Rust CLI that wraps all 18 Google Workspace APIs by fetching Discovery
Documents at runtime. MCP was briefly present in v0.7.0 (stdio transport) but removed in
v0.8.0. The goal is to re-add MCP as a production-ready **remote server** — HTTP/SSE
transport with OAuth 2.1 (Google Workspace as IdP) — suitable for multi-user enterprise
deployment on Kubernetes.

---

## Architecture

```
MCP Client (Claude Desktop / API)
      │  OAuth 2.1 + PKCE  (Google Workspace IdP)
      ▼
gws-mcp-server  (new Rust binary crate)
      │  per-user session token → per-user Google credentials
      ▼
Google Workspace APIs  (Drive, Gmail, Sheets, Calendar … 18 services)
```

One server instance serves **multiple concurrent users**. Each user authenticates with their
corporate Google account; the server issues its own short-lived Bearer session token that maps
to per-user Google credentials held in memory.

---

## New Crate: `crates/gws-mcp-server/`

A third binary crate added to the Cargo workspace. It depends on the `google-workspace`
**library** crate only — not the CLI crate.

### Module layout

```
crates/gws-mcp-server/
├── Cargo.toml
├── Dockerfile
└── src/
    ├── main.rs            # bind, pre-warm cache, spawn reaper, graceful shutdown
    ├── config.rs          # ServerConfig::from_env()
    ├── error.rs           # McpError → JSON-RPC 2.0 error codes
    ├── router.rs          # axum Router: public sub-router + authenticated sub-router
    ├── health.rs          # GET /health/live   GET /health/ready
    ├── session.rs         # UserSession, SessionStore (DashMap + trait), TTL reaper
    ├── oauth/
    │   ├── mod.rs
    │   ├── server.rs      # /authorize  /oauth/callback  /token  /userinfo  /.well-known/…
    │   ├── pkce.rs        # PKCE S256 verify helper
    │   └── google.rs      # Google code exchange, proactive token refresh via reqwest
    ├── mcp/
    │   ├── mod.rs
    │   ├── protocol.rs    # JsonRpcRequest/Response, McpTool types, MCP error codes
    │   ├── handlers.rs    # initialize, tools/list, tools/call  (all via POST /mcp)
    │   ├── tools.rs       # Discovery Document → MCP tool definitions (core new logic)
    │   └── executor.rs    # bridge: MCP tool call → google-workspace executor
    └── middleware/
        ├── auth.rs        # axum FromRequestParts extractor: Bearer → UserSession
        └── metrics.rs     # per-request counter/histogram updates
```

---

## Implementation Steps

### Step 1 — Refactor: move executor into the library crate

This is the central integration point. Currently `execute_method` lives in
`crates/google-workspace-cli/src/executor.rs`. It must move to
`crates/google-workspace/src/executor.rs` so the server crate can reuse it without
depending on the CLI binary crate.

**Changes:**

- Move `execute_method`, `build_url`, `parse_and_validate_inputs`, and response helpers from
  `crates/google-workspace-cli/src/executor.rs` into a new
  `crates/google-workspace/src/executor.rs`.
- Add a `capture_output: bool` parameter to `execute_method`. When `true`, return
  `Ok(Some(serde_json::Value))` instead of printing to stdout — the server uses this path.
- The CLI's `executor.rs` becomes a thin re-export wrapper; all existing CLI tests still pass.

**Files modified:** `crates/google-workspace/src/lib.rs`, `crates/google-workspace/src/executor.rs`
(new), `crates/google-workspace-cli/src/executor.rs`.

---

### Step 2 — OAuth 2.1 Authorization Server

The server acts as an OAuth 2.1 AS delegating identity verification to Google.

#### Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /.well-known/oauth-authorization-server` | RFC 8414 metadata (MCP client auto-discovers this) |
| `GET /authorize` | Start Google OAuth redirect; store PKCE challenge in session |
| `GET /oauth/callback` | Receive Google code, exchange tokens, redirect client with server auth code |
| `POST /token` | Exchange server auth code + PKCE verifier → Bearer session token |
| `GET /userinfo` | Return authenticated user email |

#### Flow

1. MCP client → `GET /authorize?code_challenge=<S256>…` → server redirects to Google.
2. User authenticates with corporate Google account.
3. Google → `GET /oauth/callback?code=…` → server exchanges code, creates `UserSession`,
   redirects MCP client with a server-issued one-time auth code.
4. MCP client → `POST /token` (code + PKCE verifier) → receives `{ "access_token": "<session_token>", … }`.
5. All subsequent MCP requests include `Authorization: Bearer <session_token>`.

#### Session store

```rust
pub struct UserSession {
    pub session_token: String,
    pub google_access_token: String,
    pub google_refresh_token: String,
    pub user_email: String,
    pub expires_at: Instant,
    pub scopes_granted: Vec<String>,
}
```

- Backed by `DashMap` (lock-free concurrent map, no global `RwLock` contention).
- Background `tokio::spawn` reaper evicts expired sessions every 60 s.
- `SessionStore` is a trait so a Redis backend can replace the in-memory store for
  multi-replica deployments without API changes.
- If `GWS_MCP_ALLOWED_DOMAINS` is set, reject tokens whose Google `hd` claim is not in
  the allowlist — enforces corporate Workspace domain restriction.

---

### Step 3 — MCP Protocol Layer

**Transport:** Single `POST /mcp` endpoint. Plain `application/json` for `initialize` and
`tools/list`. `text/event-stream` (SSE) optional for streaming `tools/call` progress;
start with plain JSON, add SSE as a follow-on.

#### MCP methods

| Method | Notes |
|---|---|
| `initialize` | Returns `protocolVersion: "2025-03-26"`, `capabilities: { tools: {} }` — no auth required |
| `tools/list` | Requires Bearer; walks all 18 services, converts Discovery → `Vec<McpTool>` via in-memory cache |
| `tools/call` | Requires Bearer; decodes tool name, locates `RestMethod`, calls executor, wraps result in `content: [{ type: "text", text: … }]` |

#### Tool naming convention

`gws_{service_alias}_{resource}_{subresource?}_{method}` (dots replaced by underscores)

Examples:
- `drive.files.list` → `gws_drive_files_list`
- `gmail.users.messages.send` → `gws_gmail_users_messages_send`

#### Input schema per tool (`mcp/tools.rs`)

For each `RestMethod` discovered by recursively walking `RestDescription.resources`:

```json
{
  "type": "object",
  "properties": {
    "params":   { "type": "string", "description": "JSON query/path parameters" },
    "body":     { "type": "string", "description": "JSON request body" },
    "page_all": { "type": "boolean" }
  }
}
```

- `body` only included when `method.request` is `Some(…)`.
- `page_all` only included when method has a `pageToken` parameter.
- Tool `description` = `RestMethod::description` + compact list of required path params.

This mirrors the pattern from the removed v0.7.0 MCP implementation (per CHANGELOG note
about conditional `body`/`upload`/`page_all` schema fields).

#### Server-side Discovery cache

```rust
pub struct DiscoveryCache {
    inner: DashMap<String, (Arc<RestDescription>, Instant)>,
    ttl: Duration,
}
```

- Calls `google_workspace::discovery::fetch_discovery_document(service, version, None)` —
  no filesystem cache; purely in-memory.
- Pre-warms all allowed services concurrently at startup via `futures::future::join_all`.
- `/health/ready` returns 503 until at least one service is cached.

---

### Step 4 — Configuration

All config via environment variables, consistent with the existing `.env.example` pattern:

```
GWS_MCP_HOST                  # default: 0.0.0.0
GWS_MCP_PORT                  # default: 8080
GWS_MCP_EXTERNAL_URL          # required: public base URL (used to build redirect URIs)
GWS_MCP_GOOGLE_CLIENT_ID      # required: Google OAuth2 client ID
GWS_MCP_GOOGLE_CLIENT_SECRET  # required: Google OAuth2 client secret
GWS_MCP_ALLOWED_DOMAINS       # comma-separated Workspace domains, e.g. "corp.example.com"
GWS_MCP_SESSION_TTL_SECS      # default: 3600
GWS_MCP_ALLOWED_SERVICES      # comma-separated service aliases; default: all 18
GWS_MCP_METRICS_PORT          # default: 9090 (set to 0 to disable)
GWS_MCP_LOG_LEVEL             # default: "gws_mcp_server=info"
GWS_MCP_LOG_FORMAT            # "json" (default) or "pretty"
```

---

### Step 5 — Observability

#### Prometheus metrics (served on separate `GWS_MCP_METRICS_PORT`)

```
gws_mcp_requests_total{method, status}
gws_mcp_request_duration_seconds{method}
gws_mcp_active_sessions
gws_mcp_tool_calls_total{service, tool, status}
gws_mcp_discovery_cache_hits_total{service}
gws_mcp_discovery_cache_misses_total{service}
```

Uses `metrics` crate macros (zero-overhead no-ops if no recorder installed), backed by
`metrics-exporter-prometheus`. Served on a separate port so scrape traffic is isolated
from MCP traffic.

#### Structured logging

Always-on JSON logging to stderr. Every request gets a correlation ID injected by
`tower-http`'s `RequestIdLayer`.

---

### Step 6 — Enterprise Deployment

#### Dockerfile (`crates/gws-mcp-server/Dockerfile`)

```dockerfile
FROM rust:1.83-bookworm AS builder
WORKDIR /build
COPY . .
RUN cargo build --release -p gws-mcp-server

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=builder /build/target/release/gws-mcp-server /usr/local/bin/
EXPOSE 8080 9090
ENV GWS_MCP_HOST=0.0.0.0 GWS_MCP_PORT=8080 GWS_MCP_METRICS_PORT=9090 GWS_MCP_LOG_FORMAT=json
HEALTHCHECK --interval=10s --timeout=3s CMD curl -f http://localhost:8080/health/live || exit 1
ENTRYPOINT ["/usr/local/bin/gws-mcp-server"]
```

#### Kubernetes sketch

```yaml
apiVersion: apps/v1
kind: Deployment
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: gws-mcp-server
        image: your-registry/gws-mcp-server:latest
        ports:
        - containerPort: 8080   # MCP + OAuth
        - containerPort: 9090   # Prometheus scrape
        env:
        - name: GWS_MCP_EXTERNAL_URL
          value: "https://mcp.corp.example.com"
        - name: GWS_MCP_GOOGLE_CLIENT_ID
          valueFrom: { secretKeyRef: { name: gws-mcp-oauth, key: client_id } }
        - name: GWS_MCP_GOOGLE_CLIENT_SECRET
          valueFrom: { secretKeyRef: { name: gws-mcp-oauth, key: client_secret } }
        - name: GWS_MCP_ALLOWED_DOMAINS
          value: "corp.example.com"
        livenessProbe:
          httpGet: { path: /health/live, port: 8080 }
          initialDelaySeconds: 5
        readinessProbe:
          httpGet: { path: /health/ready, port: 8080 }
          initialDelaySeconds: 10
        resources:
          requests: { cpu: "250m", memory: "256Mi" }
          limits: { cpu: "1", memory: "512Mi" }
```

#### Multi-replica session handling

With in-memory `SessionStore`, Bearer tokens are pod-local. For HA multi-replica:
1. **Simple**: `sessionAffinity: ClientIP` on the Kubernetes Service.
2. **Stateless pods**: Swap `DashMapSessionStore` for `RedisSessionStore` — the `trait
   SessionStore` abstraction is designed for this.

---

## Key New Dependencies

```toml
axum = { version = "0.8", features = ["macros"] }
tower-http = { version = "0.6", features = ["cors", "trace", "request-id", "timeout"] }
dashmap = "6"
metrics = "0.24"
metrics-exporter-prometheus = "0.16"
base64 = "0.22"
sha2 = "0.10"
async-trait = "0.1"
```

(`tokio`, `reqwest`, `serde_json`, `uuid`, `yup-oauth2`, `tracing`, `anyhow` already
present in the workspace.)

---

## Files to Modify

| File | Change |
|---|---|
| `/Cargo.toml` | Add `crates/gws-mcp-server` to workspace members |
| `crates/google-workspace/src/lib.rs` | `pub mod executor` |
| `crates/google-workspace-cli/src/executor.rs` | Thin re-export from lib |
| `.env.example` | Add `GWS_MCP_*` variables |

## Files to Create

- `crates/gws-mcp-server/Cargo.toml`
- `crates/gws-mcp-server/Dockerfile`
- `crates/gws-mcp-server/src/` — all modules listed above
- `crates/google-workspace/src/executor.rs` — extracted execution engine
- `.changeset/<name>.md` — required by CI policy (AGENTS.md)

---

## Implementation Order

1. Executor extraction → move `execute_method` to lib crate; all tests pass
2. Skeleton → workspace + `Cargo.toml` + `config.rs` + bind loop + health endpoints
3. Session store → `UserSession`, `DashMap` store, TTL reaper, `trait SessionStore`
4. Discovery cache → in-memory `DiscoveryCache`, pre-warm at startup
5. OAuth server → PKCE helpers, Google token exchange, all 5 endpoints
6. MCP protocol → types, tools converter, handlers, executor bridge
7. Middleware + observability → auth extractor, Prometheus metrics
8. Router assembly → wire all routes, CORS, tracing middleware
9. Dockerfile + changeset

---

## Verification

```bash
# Build
cargo build -p gws-mcp-server

# Tests
cargo test -p gws-mcp-server
cargo test -p google-workspace        # ensure executor extraction didn't break lib
cargo test -p google-workspace-cli    # ensure CLI still works

# Run locally
GWS_MCP_GOOGLE_CLIENT_ID=... GWS_MCP_GOOGLE_CLIENT_SECRET=... \
GWS_MCP_EXTERNAL_URL=http://localhost:8080 \
cargo run -p gws-mcp-server

# Verify OAuth discovery
curl http://localhost:8080/.well-known/oauth-authorization-server

# Verify readiness
curl http://localhost:8080/health/ready

# MCP initialize (no auth required)
curl -X POST http://localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{}}}'

# tools/list (after full OAuth flow)
curl -X POST http://localhost:8080/mcp \
  -H 'Authorization: Bearer <session_token>' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'

# Docker build
docker build -f crates/gws-mcp-server/Dockerfile -t gws-mcp-server .
```
