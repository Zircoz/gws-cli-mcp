# Google Workspace CLI - Remote MCP Server

This directory provides a **Remote Model Context Protocol (MCP) Server** wrapper for the `gws` (Google Workspace) CLI. It allows you to expose the full functionality of the `gws` CLI to remote MCP clients (such as Claude, custom agents, etc.) via an HTTP Server-Sent Events (SSE) transport.

It is designed for **Enterprise Deployments** and includes OAuth 2.1 authorization integration, acting as an OAuth 2.1 Resource Server to securely validate clients.

## Architecture

1. **MCP Client** (e.g. an AI agent) obtains an OAuth 2.1 Bearer token from your Enterprise Identity Provider (IdP) (e.g., Okta, Auth0, Keycloak).
2. The Client connects to this Remote MCP Server via HTTP SSE (`/mcp/sse`), passing the `Authorization: Bearer <token>` header.
3. The Server acts as a **Resource Server**, fetching the JWKS (JSON Web Key Set) from your IdP to cryptographically verify the token's signature, issuer, audience, and expiration.
4. Once authenticated, the Client can execute tools (e.g., `execute_gws`) which securely invoke the underlying `gws` CLI.

## Implementations

We provide two reference implementations to suit your infrastructure:

- **Python (Recommended)**: Built using `FastMCP` (from the official `mcp` SDK) and `FastAPI`. Best for high-concurrency async operations.
- **Node.js**: Built using Express and the `@modelcontextprotocol/sdk`.

## Enterprise Deployment Best Practices

### Prerequisites
- The `gws` CLI must be installed and authenticated on the host machine running the server. The server invokes `gws` as a subprocess. You can use Service Accounts (`GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE`) for non-interactive enterprise use.
- An Enterprise IdP configured to issue JWTs to your clients.

### 1. Configuration (Environment Variables)

Both the Python and Node.js servers rely on the following environment variables for OAuth 2.1 enforcement:

| Variable | Description | Default |
|---|---|---|
| `ENABLE_AUTH` | Set to `true` to enable OAuth 2.1 Bearer token validation. | `false` (for local testing) |
| `OAUTH_ISSUER` | The URL of your Authorization Server (IdP). | `https://your-idp.example.com/` |
| `OAUTH_AUDIENCE` | The expected `aud` claim in the JWT. Usually the Canonical Server URI of this MCP server. | `https://mcp.example.com` |
| `JWKS_URI` | The URI to fetch public keys to verify JWT signatures. | `$OAUTH_ISSUER.well-known/jwks.json` |
| `PORT` | The HTTP port to bind to. | `8000` |

### 2. Running the Python Server

```bash
cd python
pip install -r requirements.txt

# Start the server (uvicorn)
ENABLE_AUTH=true OAUTH_ISSUER="https://my-okta.com/oauth2/default/" uvicorn app:app_with_auth --host 0.0.0.0 --port 8000
```

*Note: For production, consider using `gunicorn` with `uvicorn` workers.*

### 3. Running the Node.js Server

```bash
cd nodejs
npm install

# Start the server
ENABLE_AUTH=true OAUTH_ISSUER="https://my-okta.com/oauth2/default/" node server.js
```

### 4. Health Checks

For Kubernetes or Load Balancers (AWS ALB, etc.), both servers expose a public `/health` endpoint that bypasses OAuth verification:

```bash
curl -I http://localhost:8000/health
# HTTP/1.1 200 OK
```

### 5. Logging and Observability

- **Python**: Uses the standard `logging` module. Configure `logging.basicConfig` in `app.py` to emit JSON logs if your log aggregator (Datadog, Splunk, ELK) prefers structured logs.
- **Node.js**: Currently uses `console.log`. For enterprise use, consider replacing it with a structured logger like `pino` or `winston`.
- The `gws` CLI invocations are logged. Ensure you do not log the `--params` if they contain PII or sensitive data.

### 6. Client Connection

MCP Clients must connect to the following endpoints using the HTTP SSE Transport:

- **SSE Endpoint**: `GET /mcp/sse`
- **Messages Endpoint**: `POST /mcp/messages?sessionId=<uuid>` (The exact URL is returned by the server upon connecting to the SSE endpoint).

Clients **must** include the header:
```
Authorization: Bearer <YOUR_ACCESS_TOKEN>
```
If the token is invalid or missing, the server will return a `401 Unauthorized` with a `WWW-Authenticate` header conforming to [RFC 6750](https://datatracker.ietf.org/doc/html/rfc6750) and the MCP Authorization Spec.
