# Google Workspace CLI - Remote MCP Server

This directory provides a **Remote Model Context Protocol (MCP) Server** wrapper for the `gws` (Google Workspace) CLI. It allows you to expose the full functionality of the `gws` CLI to remote MCP clients (such as Claude, custom agents, etc.) via a Streamable HTTP transport.

It is designed for **Enterprise Deployments** and includes OAuth 2.1 authorization integration, acting as an OAuth 2.1 Resource Server to securely validate clients.

> For a comprehensive enterprise deployment walkthrough (Kubernetes, Cloud Run, IdP integration, security hardening), see [docs/enterprise-deployment.md](../docs/enterprise-deployment.md).

## Architecture

1. **MCP Client** (e.g. an AI agent) obtains an OAuth 2.1 Bearer token from your Enterprise Identity Provider (IdP) (e.g., Okta, Auth0, Keycloak).
2. The Client connects to this Remote MCP Server via `POST /mcp`, passing the `Authorization: Bearer <token>` header.
3. The Server acts as a **Resource Server**, fetching the JWKS (JSON Web Key Set) from your IdP to cryptographically verify the token's signature, issuer, audience, and expiration.
4. Once authenticated, the Client can execute tools (e.g., `execute_gws`) which securely invoke the underlying `gws` CLI.

## Implementations

We provide two reference implementations to suit your infrastructure:

- **Python (Recommended)**: Built using `FastMCP` (from the official `mcp` SDK) and `FastAPI`. Best for high-concurrency async operations.
- **Node.js**: Built using Express and the `@modelcontextprotocol/sdk`.

## Enterprise Deployment Best Practices

### Prerequisites
- The `gws` CLI must be installed and authenticated on the host machine running the server. The server invokes `gws` as a subprocess. Use Service Accounts (`GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE`) for non-interactive enterprise use.
- An Enterprise IdP configured to issue JWTs to your clients.

### 1. Configuration (Environment Variables)

Both the Python and Node.js servers rely on the following environment variables:

| Variable | Description | Default |
|---|---|---|
| `ENABLE_AUTH` | Set to `true` to enable OAuth 2.1 Bearer token validation. | `false` (for local testing) |
| `OAUTH_ISSUER` | The URL of your Authorization Server (IdP). | `https://your-idp.example.com/` |
| `OAUTH_AUDIENCE` | The expected `aud` claim in the JWT. Usually the Canonical Server URI of this MCP server. | `https://mcp.example.com` |
| `JWKS_URI` | The URI to fetch public keys to verify JWT signatures. | `$OAUTH_ISSUER.well-known/jwks.json` |
| `REQUIRED_SCOPES` | Space-separated list: the JWT must contain at least one of these scopes. *(Python only)* | `gws:read gws:write` |
| `GWS_ALLOWED_SERVICES` | Comma-separated list of `gws` service names clients may invoke. Set to `*` or omit to allow all. | *(unrestricted)* |
| `PORT` | The HTTP port to bind to. | `8000` |

### 2. Restricting which services MCP clients can call

Set `GWS_ALLOWED_SERVICES` to limit the `gws` services accessible through this server. Requests for any service not in the list are rejected **before** a subprocess is spawned, so there is no risk of the CLI accidentally executing a disallowed command.

```bash
# Only allow Drive, Gmail, and Calendar
GWS_ALLOWED_SERVICES=drive,gmail,calendar

# Broader deployment
GWS_ALLOWED_SERVICES=drive,gmail,calendar,sheets,docs,tasks

# Unrestricted (default — not recommended for production)
GWS_ALLOWED_SERVICES=*
```

**Error response when a client requests a blocked service:**

```
Error: Service 'admin-reports' is not permitted on this server.
Allowed services: calendar, drive, gmail
```

**Combining layers for defence-in-depth:**

1. **Google OAuth scopes** on the `gws` credentials — define the maximum Google Workspace permissions at the API level (e.g., `drive.readonly` credentials block all Drive write calls regardless of what the MCP server permits).
2. **`GWS_ALLOWED_SERVICES`** — restrict which services the MCP server will proxy.
3. **`REQUIRED_SCOPES`** on the IdP JWT — control which agents can connect to the server at all.

### 3. Running the Python Server

```bash
cd python
pip install -r requirements.txt

# Start the server
ENABLE_AUTH=true \
OAUTH_ISSUER="https://my-okta.com/oauth2/default/" \
GWS_ALLOWED_SERVICES=drive,gmail,calendar \
uvicorn app:app_with_auth --host 0.0.0.0 --port 8000
```

For production, use `gunicorn` with `uvicorn` workers:

```bash
gunicorn app:app_with_auth -k uvicorn.workers.UvicornWorker -w 4 --bind 0.0.0.0:8000
```

### 4. Running the Node.js Server

```bash
cd nodejs
npm install

ENABLE_AUTH=true \
OAUTH_ISSUER="https://my-okta.com/oauth2/default/" \
GWS_ALLOWED_SERVICES=drive,gmail,calendar \
node server.js
```

### 5. Health Checks

For Kubernetes or Load Balancers (AWS ALB, etc.), both servers expose a public `/health` endpoint that bypasses OAuth verification:

```bash
curl -I http://localhost:8000/health
# HTTP/1.1 200 OK
```

### 6. Logging and Observability

- **Python**: Uses the standard `logging` module. Configure `logging.basicConfig` in `app.py` to emit JSON logs if your log aggregator (Datadog, Splunk, ELK) prefers structured logs.
- **Node.js**: Currently uses `console.log`. For enterprise use, consider replacing it with a structured logger like `pino` or `winston`.
- The `gws` CLI invocations are logged. Ensure you do not log the `--params` content if it contains PII or sensitive data.

### 7. Client Connection

MCP Clients must connect using the **Streamable HTTP Transport** (supported by `@modelcontextprotocol/sdk` >= 1.4).

- **Streamable Endpoint**: `POST /mcp`

Clients **must** include the header:
```
Authorization: Bearer <YOUR_ACCESS_TOKEN>
```
If the token is invalid or missing, the server returns `401 Unauthorized` with a `WWW-Authenticate` header conforming to [RFC 6750](https://datatracker.ietf.org/doc/html/rfc6750) and the MCP Authorization Spec.
