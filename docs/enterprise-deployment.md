# Enterprise Deployment Guide

This guide covers how to deploy `gws` and its Remote MCP Server in an enterprise environment — including authentication for non-interactive hosts, scope restriction to limit what MCP clients can do, security hardening, and operational guidance for Kubernetes/Cloud Run.

## Contents

- [Architecture overview](#architecture-overview)
- [Prerequisites](#prerequisites)
- [GCP project setup](#gcp-project-setup)
- [gws authentication for servers](#gws-authentication-for-servers)
- [Deploying the Remote MCP Server](#deploying-the-remote-mcp-server)
- [Restricting MCP scopes and services](#restricting-mcp-scopes-and-services)
- [Enterprise IdP integration](#enterprise-idp-integration)
- [Security hardening](#security-hardening)
- [Observability](#observability)
- [Kubernetes / Cloud Run reference](#kubernetes--cloud-run-reference)
- [Troubleshooting](#troubleshooting)

---

## Architecture overview

```
┌───────────────────────────────────────────────────────────────────────┐
│  MCP Client (Claude, custom agent)                                    │
│                                                                       │
│  1. Obtains JWT from enterprise IdP (Okta / Auth0 / Keycloak / GCP)  │
│  2. POST /mcp  Authorization: Bearer <jwt>                            │
└──────────────────────────────┬────────────────────────────────────────┘
                               │  HTTPS / Streamable HTTP
┌──────────────────────────────▼────────────────────────────────────────┐
│  Remote MCP Server  (Python)                                          │
│                                                                       │
│  ● Validates JWT against IdP JWKS (signature, issuer, audience)       │
│  ● Enforces service allowlist  (GWS_ALLOWED_SERVICES)                 │
│  ● Spawns  gws <service> <command>  as a subprocess                   │
└──────────────────────────────┬────────────────────────────────────────┘
                               │  subprocess
┌──────────────────────────────▼────────────────────────────────────────┐
│  gws CLI                                                              │
│                                                                       │
│  ● Authenticates to Google using a Service Account or ADC             │
│  ● Calls the Google Workspace REST API                                │
│  ● Returns structured JSON to stdout                                  │
└───────────────────────────────────────────────────────────────────────┘
```

---

## Prerequisites

- **`gws` binary** installed on the host and accessible via `PATH`.
- **Google Cloud project** with the Workspace APIs you need enabled.
- **Enterprise IdP** (Okta, Auth0, Keycloak, or Google's own IAP) configured to issue JWTs to your AI agents/clients.
- Docker / Kubernetes, or Google Cloud Run for container hosting.

---

## GCP project setup

### Enable required APIs

Enable only the APIs your deployment actually needs:

```bash
gcloud services enable \
  drive.googleapis.com \
  gmail.googleapis.com \
  calendar-json.googleapis.com \
  sheets.googleapis.com \
  --project=<PROJECT_ID>
```

Using `gws auth setup` automates this if you have `gcloud` installed:

```bash
gws auth setup
```

### OAuth consent screen

For enterprise Workspace domains, set the app type to **Internal** so only users within the domain can authorize. This bypasses the 25-scope limit that applies to unverified external apps.

1. Open **APIs & Services → OAuth consent screen** in the Cloud Console.
2. Set **User type** to **Internal**.
3. Fill in app name and support email.
4. Add the scopes your deployment needs (see [Restricting MCP scopes and services](#restricting-mcp-scopes-and-services)).

---

## gws authentication for servers

Headless servers (containers, VMs, CI) cannot open a browser. Use one of these non-interactive auth methods:

### Option A: Service Account (recommended for server-to-server)

```bash
# Create a service account
gcloud iam service-accounts create gws-mcp-server \
  --display-name="GWS MCP Server" \
  --project=<PROJECT_ID>

# Grant it the minimum IAM roles for the APIs you need
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="serviceAccount:gws-mcp-server@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator"

# Create and download a key
gcloud iam service-accounts keys create sa-key.json \
  --iam-account=gws-mcp-server@<PROJECT_ID>.iam.gserviceaccount.com

# Point gws at the key
export GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=/etc/gws/sa-key.json
```

Store the key in a Kubernetes Secret or a secret manager (Google Secret Manager, HashiCorp Vault) rather than baking it into the container image.

For Google Workspace data (Gmail, Drive, Calendar), the service account may need **domain-wide delegation** so it can impersonate domain users:

1. In the Google Admin console → **Security → API controls → Domain-wide delegation**, add the service account's client ID and the OAuth scopes it should be allowed to impersonate.
2. In `gws` commands, pass `--params '{"userId": "user@example.com"}'` (or the equivalent resource path) to act on behalf of that user.

### Option B: Export credentials from an interactive machine

Complete the OAuth flow once on a machine with a browser, then export and move the encrypted credentials:

```bash
# On the machine with a browser:
gws auth login --scopes drive,gmail,calendar
gws auth export --unmasked > credentials.json

# On the headless host:
export GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=/etc/gws/credentials.json
```

Credentials are in the standard `authorized_user` JSON format and can be stored in Kubernetes Secrets.

### Option C: Workload Identity (GKE)

When running in GKE, attach the Kubernetes Service Account to a GCP Service Account via Workload Identity. `gws` automatically picks up Application Default Credentials:

```bash
gcloud iam service-accounts add-iam-policy-binding gws-mcp-server@<PROJECT_ID>.iam.gserviceaccount.com \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:<PROJECT_ID>.svc.id.goog[<NAMESPACE>/<KSA_NAME>]"

kubectl annotate serviceaccount <KSA_NAME> \
  iam.gke.io/gcp-service-account=gws-mcp-server@<PROJECT_ID>.iam.gserviceaccount.com
```

No credential file is needed — `gws` discovers the token from the GKE metadata server via ADC.

---

## Deploying the Remote MCP Server

The `remote-mcp-server/` directory contains a Python (FastAPI/FastMCP) reference implementation.

```bash
cd remote-mcp-server/python
pip install -r requirements.txt

# Minimal launch (auth disabled, for local testing):
# ENABLE_AUTH must be set explicitly — the server refuses to start otherwise.
ENABLE_AUTH=false uvicorn app:app_with_auth --host 0.0.0.0 --port 8000

# Production launch (gunicorn is not in requirements.txt — install separately):
pip install gunicorn
ENABLE_AUTH=true \
OAUTH_ISSUER=https://my-okta.com/oauth2/default \
JWKS_URI=https://my-okta.com/oauth2/default/v1/keys \
OAUTH_AUDIENCE=https://mcp.example.com \
GWS_ALLOWED_SERVICES=drive,gmail,calendar \
GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=/etc/gws/sa-key.json \
gunicorn app:app_with_auth -k uvicorn.workers.UvicornWorker -w 4 --bind 0.0.0.0:8000
```

Multiple `-w` workers are safe here because `server.py` constructs `FastMCP(..., stateless_http=True)` — each request is handled independently rather than relying on server-side session state pinned to whichever worker process created it.

### Docker

```dockerfile
FROM python:3.12-slim

# curl is needed to fetch the gws binary below
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install gws binary
RUN curl -fsSL https://github.com/googleworkspace/cli/releases/latest/download/google-workspace-cli-x86_64-unknown-linux-gnu.tar.gz \
    | tar -xz -C /usr/local/bin

WORKDIR /app
COPY remote-mcp-server/python/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY remote-mcp-server/python/ .

# Credentials are injected at runtime — never bake them into the image
ENV GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=/etc/gws/credentials.json

# ENABLE_AUTH is not a secret, so it's safe to default it here — the image
# ships secure-by-default. It (and OAUTH_ISSUER, JWKS_URI, OAUTH_AUDIENCE,
# GWS_ALLOWED_SERVICES, etc.) can still be overridden at `docker run` time
# with -e, but the server now refuses to start if ENABLE_AUTH is left unset
# entirely, so every image built from this recipe must carry some value.
ENV ENABLE_AUTH=true

EXPOSE 8000
CMD ["gunicorn", "app:app_with_auth", "-k", "uvicorn.workers.UvicornWorker", \
     "-w", "4", "--bind", "0.0.0.0:8000", "--access-logfile", "-"]
```

---

## Restricting MCP scopes and services

Scope restriction operates at two complementary layers:

### Layer 1: gws credentials (Google OAuth scopes)

The credentials used by `gws` (service account key or OAuth refresh token) define the **maximum** Google Workspace permissions available. To enforce a read-only deployment:

```bash
# Authenticate with read-only Google OAuth scopes
gws auth login --readonly
gws auth export --unmasked > credentials-readonly.json
```

With read-only credentials, any `execute_gws` call that attempts a write operation (e.g., `gmail messages delete`) will receive a 403 from Google's API — even if the MCP server otherwise permits the command.

### Layer 2: Server-side service allowlist (recommended)

Set `GWS_ALLOWED_SERVICES` to a comma-separated list of `gws` service names. Requests for unlisted services are rejected before any subprocess is spawned:

```bash
# Allow only Drive and Gmail
GWS_ALLOWED_SERVICES=drive,gmail

# Allow a broader set for a team productivity deployment
GWS_ALLOWED_SERVICES=drive,gmail,calendar,sheets,docs,tasks

# Unrestricted (default — not recommended for production)
GWS_ALLOWED_SERVICES=*
```

The check is alias-aware and closes the `service:version` Discovery escape hatch: a service name containing `:` (e.g. `compute:v1`) is always rejected, and aliases (`reports` / `admin-reports`) are normalized to the same canonical name before comparison, so allowing one spelling reliably allows both. Even `GWS_ALLOWED_SERVICES=*` only ever reaches `gws`'s own named service list — not arbitrary Google Discovery APIs — because the `:` syntax is rejected regardless of the allowlist setting.

**Available service names:**

| Name | API |
|------|-----|
| `drive` | Google Drive v3 |
| `gmail` | Gmail v1 |
| `calendar` | Google Calendar v3 |
| `sheets` | Google Sheets v4 |
| `docs` | Google Docs v1 |
| `slides` | Google Slides v1 |
| `tasks` | Google Tasks v1 |
| `people` | People API v1 |
| `chat` | Google Chat v1 |
| `classroom` | Google Classroom v1 |
| `forms` | Google Forms v1 |
| `keep` | Google Keep v1 |
| `meet` | Google Meet v2 |
| `events` | Workspace Events v1 |
| `admin-reports` | Admin SDK Reports v1 |
| `modelarmor` | Model Armor v1 |
| `script` | Apps Script v1 |
| `workflow` | Workflow helpers |

**Example — error response when a service is blocked:**

```json
{
  "content": [{
    "type": "text",
    "text": "Error: Service 'admin-reports' is not permitted on this server.\nAllowed services: calendar, drive, gmail"
  }]
}
```

### Layer 3: IdP-controlled scope mapping

Your enterprise IdP grants MCP clients a JWT whose `scope` claim indicates what the client is authorized to do (e.g., `gws.drive gws.gmail.readonly`). Configure `REQUIRED_SCOPES` on the Python server so that only clients presenting the right JWT scopes can connect at all:

```bash
# Require the client to hold at least one of these custom scopes
REQUIRED_SCOPES="gws:read gws:write"
```

For fine-grained per-client restrictions, issue different JWTs to different agent identities:

| Agent / persona | JWT scopes | `GWS_ALLOWED_SERVICES` on their dedicated server |
|----------------|-----------|--------------------------------------------------|
| Readonly research agent | `gws:read` | `drive,gmail,calendar` with read-only `gws` credentials |
| Team assistant | `gws:team` | `drive,gmail,calendar,sheets,docs,tasks` |
| IT admin agent | `gws:admin` | `admin-reports,chat,classroom,events` |

Deploy a separate MCP server instance per role, each with its own `GWS_ALLOWED_SERVICES` and its own `gws` credentials carrying only the necessary Google OAuth scopes.

### Layer 4: True per-user credential isolation (`GWS_PER_USER_TOKEN`)

The per-role deployment pattern above still runs every caller of a given server instance as that instance's one host identity — it partitions by *role*, not by *individual user*. If your deployment instead needs each authenticated end user to act as themselves against Google (not as a shared service identity), set `GWS_PER_USER_TOKEN=true`. Each request then executes with the calling user's own Google access token — extracted per-request from the validated JWT (or a forwarded header) and injected only into that one `gws` child process — instead of the host's credentials. This requires `ENABLE_AUTH=true`, since it depends on a validated caller identity to isolate by. See the Python server's README §3.5 for the full mechanism and configuration.

---

## Enterprise IdP integration

The Remote MCP Server acts as an **OAuth 2.1 Resource Server**. It validates incoming JWTs using the IdP's JWKS endpoint.

### Environment variables

| Variable | Description | Example |
|---|---|---|
| `ENABLE_AUTH` | Enable JWT validation. Must be exactly `true` or `false` — the server refuses to start if it's unset/ambiguous, unless `ALLOW_INSECURE_NO_AUTH=true` is also set (local testing only). | `true` |
| `ALLOW_INSECURE_NO_AUTH` | Explicit, loudly-logged opt-out letting the server start with auth disabled when `ENABLE_AUTH` is unset/ambiguous. Never set this for a production deployment. | `false` |
| `OAUTH_ISSUER` | IdP issuer URL (must exactly match the token's `iss` claim, including trailing-slash presence/absence) | `https://my-company.okta.com/oauth2/default` |
| `OAUTH_AUDIENCE` | Expected `aud` claim (canonical server URI) | `https://mcp.example.com` |
| `JWKS_URI` | JWKS endpoint for public key fetch. Defaults to `<OAUTH_ISSUER>/.well-known/jwks.json`, which is correct for Auth0-style IdPs but **not** Okta (see below) — set it explicitly whenever your IdP doesn't use that path. | `https://my-company.okta.com/oauth2/default/v1/keys` |
| `REQUIRED_SCOPES` | Space-separated scopes the JWT must contain ≥1 of. An explicit empty value with `ENABLE_AUTH=true` refuses to start unless `REQUIRE_SCOPES=false` is also set. | `gws:read gws:write` |
| `REQUIRE_SCOPES` | Set to `false` to explicitly opt out of scope enforcement (only needed alongside an intentional empty `REQUIRED_SCOPES`). | `true` |
| `GWS_PER_USER_TOKEN` | Multi-tenant mode: scope each request to the calling user's own Google identity instead of the host's. Requires `ENABLE_AUTH=true`. See the Python server's README §3.5. | `false` |
| `GWS_USER_TOKEN_CLAIM` / `GWS_USER_TOKEN_HEADER` | Where to read the caller's Google token from when `GWS_PER_USER_TOKEN=true` — a JWT claim name, or (default) the `X-GWS-User-Token` forwarded header. | *(unset)* / `X-GWS-User-Token` |
| `EXPOSE_API_DOCS` | Mount `/docs` and `/openapi.json`, unauthenticated. Disabled by default. | `false` |
| `PORT` | Server port. Only honored by the `python app.py` dev-mode entry point — the production `uvicorn`/`gunicorn` commands and the Dockerfile `CMD` bind an explicit port and must be edited to match if you change it. | `8000` |

### Okta

1. Create a new **API Service** application in Okta.
2. Add a custom authorization server (or use the `default` server).
3. Define custom scopes: `gws:read`, `gws:write`, etc.
4. Create a policy and rule granting the agent's client credentials the scopes.
5. Set `OAUTH_ISSUER` to the authorization server issuer URL, **without** a trailing slash (it must exactly match the `iss` claim Okta puts in issued tokens).
6. Set `JWKS_URI` explicitly to `<issuer>/v1/keys` — Okta does not serve JWKS at the `.well-known/jwks.json` path this server defaults to.

### Auth0

1. Create a Machine-to-Machine application.
2. Create an API with identifier matching `OAUTH_AUDIENCE`.
3. Add permissions (`gws:read`, `gws:write`) to the API.
4. Authorize the M2M application and assign the permissions.
5. Set `OAUTH_ISSUER` to `https://<your-tenant>.auth0.com/`.

### Google IAP (Identity-Aware Proxy)

For Google Cloud deployments you can use IAP as the OAuth gatekeeper in front of the MCP server. IAP issues OIDC tokens; configure the server to validate against Google's JWKS:

```bash
OAUTH_ISSUER=https://accounts.google.com
OAUTH_AUDIENCE=/projects/<PROJECT_NUMBER>/apps/<BACKEND_SERVICE_ID>
JWKS_URI=https://www.googleapis.com/oauth2/v3/certs
```

---

## Security hardening

### Never bake credentials into the image

Use a secret manager:

```yaml
# Kubernetes Secret
apiVersion: v1
kind: Secret
metadata:
  name: gws-credentials
type: Opaque
data:
  sa-key.json: <base64-encoded-key>
---
# Mount it in the Deployment
volumes:
  - name: gws-creds
    secret:
      secretName: gws-credentials
volumeMounts:
  - name: gws-creds
    mountPath: /etc/gws
    readOnly: true
```

### Enable Model Armor for response sanitization

`gws` integrates with [Google Cloud Model Armor](https://cloud.google.com/security/products/model-armor) to sanitize API responses before they reach an AI agent. Enable it to protect against prompt injection via malicious Workspace content:

```bash
GOOGLE_WORKSPACE_CLI_SANITIZE_TEMPLATE=projects/<P>/locations/<L>/templates/<T>
GOOGLE_WORKSPACE_CLI_SANITIZE_MODE=block   # reject the response entirely on violation
```

Add `--sanitize` to any `gws` call inside `execute_gws`, or set the environment variable so the default applies to every call.

### Bind to localhost behind a reverse proxy

Do not expose the MCP server directly on `0.0.0.0` in production. Instead, bind to `127.0.0.1` and front it with nginx or a cloud load balancer that terminates TLS:

```bash
gunicorn app:app_with_auth -k uvicorn.workers.UvicornWorker --bind 127.0.0.1:8000
```

### TLS

Always terminate TLS at the load balancer or ingress layer. Connections from MCP clients without TLS expose the Bearer token in plaintext.

### Input validation

The `gws` binary enforces its own input validation (path traversal rejection, URL encoding, resource name validation). Do not bypass it by constructing shell commands manually — always invoke it via an argument list (`asyncio.create_subprocess_exec("gws", service, ...)`) rather than a shell string.

### Principle of least privilege for service accounts

- Grant the service account only the IAM roles it needs (`roles/drive.readonly` rather than `roles/drive.admin`).
- Set Google OAuth scopes on the credentials to only what is needed (pass `--readonly` to `gws auth login`, or enumerate specific scopes with `--scopes drive.readonly,gmail.readonly`).
- Rotate service account keys on a schedule (or use Workload Identity, which eliminates long-lived keys).

---

## Observability

### Structured logging (Python)

Replace `basicConfig` with a JSON formatter:

```python
import json, logging, sys

class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "ts": self.formatTime(record),
            "level": record.levelname,
            "msg": record.getMessage(),
            "logger": record.name,
        })

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logging.root.addHandler(handler)
logging.root.setLevel(logging.INFO)
```

### gws CLI logs

Enable `gws` JSON-line logs to a directory for later analysis:

```bash
GOOGLE_WORKSPACE_CLI_LOG=gws=info
GOOGLE_WORKSPACE_CLI_LOG_FILE=/var/log/gws
```

Log files rotate daily. Each line is a JSON object.

### Health check endpoint

The server exposes `GET /health` — returns `200 {"status":"ok"}`. Use it for:
- Kubernetes `livenessProbe` and `readinessProbe`
- ALB / Cloud Load Balancing health checks
- Uptime monitors

---

## Kubernetes / Cloud Run reference

### Kubernetes Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: gws-mcp-server
spec:
  replicas: 2
  selector:
    matchLabels:
      app: gws-mcp-server
  template:
    metadata:
      labels:
        app: gws-mcp-server
    spec:
      serviceAccountName: gws-ksa   # if using Workload Identity
      containers:
        - name: gws-mcp-server
          image: your-registry/gws-mcp-server:latest
          ports:
            - containerPort: 8000
          env:
            - name: ENABLE_AUTH
              value: "true"
            - name: OAUTH_ISSUER
              value: "https://my-company.okta.com/oauth2/default"
            - name: JWKS_URI
              value: "https://my-company.okta.com/oauth2/default/v1/keys"
            - name: OAUTH_AUDIENCE
              value: "https://mcp.example.com"
            - name: GWS_ALLOWED_SERVICES
              value: "drive,gmail,calendar,sheets"
            - name: GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE
              value: /etc/gws/sa-key.json
            - name: GOOGLE_WORKSPACE_CLI_SANITIZE_MODE
              value: "block"
          volumeMounts:
            - name: gws-creds
              mountPath: /etc/gws
              readOnly: true
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 15
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 3
            periodSeconds: 10
      volumes:
        - name: gws-creds
          secret:
            secretName: gws-credentials
---
apiVersion: v1
kind: Service
metadata:
  name: gws-mcp-server
spec:
  selector:
    app: gws-mcp-server
  ports:
    - port: 80
      targetPort: 8000
```

### Google Cloud Run

```bash
gcloud run deploy gws-mcp-server \
  --image=your-registry/gws-mcp-server:latest \
  --region=us-central1 \
  --platform=managed \
  --no-allow-unauthenticated \
  --set-env-vars="ENABLE_AUTH=true,OAUTH_ISSUER=https://my-company.okta.com/oauth2/default,JWKS_URI=https://my-company.okta.com/oauth2/default/v1/keys,GWS_ALLOWED_SERVICES=drive,gmail,calendar,GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=/etc/gws/sa-key.json" \
  --set-secrets="/etc/gws/sa-key.json=gws-sa-key:latest" \
  --service-account=gws-mcp-server@<PROJECT_ID>.iam.gserviceaccount.com
```

`--set-secrets` here mounts the secret as a *file* at `/etc/gws/sa-key.json` (`path=secret:version` syntax) — `gws` requires `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE` to point at a file on disk, not the raw secret value, so injecting the secret directly into that env var (`GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=gws-sa-key:latest`) would put the JSON key body itself in the variable and every `gws` call would fail with "file does not exist".

Use `--no-allow-unauthenticated` to enforce Google IAP in front, or manage JWT validation inside the server with `ENABLE_AUTH=true`.

---

## Troubleshooting

### `gws` binary not found

```
Error: 'gws' binary not found. Install it and ensure it is on PATH.
```

Install `gws` on the host machine or in the container image before starting the server:

```bash
# Via npm
npm install -g @googleworkspace/cli

# Or download a pre-built binary:
curl -fsSL https://github.com/googleworkspace/cli/releases/latest/download/google-workspace-cli-x86_64-unknown-linux-gnu.tar.gz \
  | tar -xz -C /usr/local/bin
```

### `401 Unauthorized` from MCP server

- Confirm `ENABLE_AUTH=true` and that the client includes `Authorization: Bearer <token>`.
- Verify `OAUTH_ISSUER` and `OAUTH_AUDIENCE` match the values in the JWT (`iss` and `aud` claims).
- Check that `JWKS_URI` is reachable from the server host.
- Confirm the JWT is not expired.

### `Error: Service '…' is not permitted`

The agent is calling a service not listed in `GWS_ALLOWED_SERVICES`. Either add the service to the allowlist, or update the agent's instructions to use only permitted services.

### Google API `403 accessNotConfigured`

The API is not enabled for the GCP project. Enable it:

```bash
gcloud services enable <api>.googleapis.com --project=<PROJECT_ID>
```

Or follow the `enable_url` in the error JSON output.

### Google API `403 insufficientPermissions`

The service account does not have the required IAM role or Google OAuth scope for the requested operation. Check:

1. The IAM bindings on the GCP project/resource.
2. The scopes embedded in the `gws` credentials (`gws auth status` reports the token's granted scopes).
3. Whether domain-wide delegation is configured for Workspace user data.

### Too many OAuth scopes / consent error

Unverified external OAuth apps are limited to ~25 scopes. For enterprise deployments, set the OAuth consent screen to **Internal** (Workspace org only) to lift this limit, or select only the specific scopes you need:

```bash
gws auth login --scopes drive,gmail,calendar
```
