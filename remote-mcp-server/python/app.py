import logging
import os
from contextlib import asynccontextmanager
from typing import Dict, Optional
import anyio
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse
import jwt
from jwt import PyJWKClient

# Configure logging before importing server — server.py logs two INFO lines
# at import time (its allowlist summary), and this basicConfig() call must
# already be in effect for those to print at INFO rather than being dropped
# by the logging module's default WARNING level.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gws-mcp-server")

# Import our FastMCP server instance, along with the per-request token
# machinery it owns: USER_TOKEN (the ContextVar this middleware populates)
# and PER_USER_TOKEN_MODE (whether per-user credential isolation is on).
from server import mcp, USER_TOKEN, PER_USER_TOKEN_MODE

# OAuth 2.1 Configuration
OAUTH_ISSUER = os.getenv("OAUTH_ISSUER", "https://your-idp.example.com/")
OAUTH_AUDIENCE = os.getenv("OAUTH_AUDIENCE", "https://mcp.example.com") # Canonical Server URI
# Trailing-slash-tolerant default: many IdPs (e.g. Auth0) publish JWKS at
# "<issuer>/.well-known/jwks.json"; Okta does not use this path at all (its
# JWKS lives at "<issuer>/v1/keys") and JWKS_URI must be set explicitly for
# it — see docs/enterprise-deployment.md's Okta section.
JWKS_URI = os.getenv("JWKS_URI", f"{OAUTH_ISSUER.rstrip('/')}/.well-known/jwks.json")

# --- ENABLE_AUTH: fail closed ---
#
# A missing/unset ENABLE_AUTH must never silently disable authentication on a
# network-exposed server. ENABLE_AUTH must be exactly "true" or exactly
# "false" (case-insensitive); anything else — unset, empty, "1", "yes",
# typos — is a startup error unless the operator explicitly opts into running
# without a recognized value via ALLOW_INSECURE_NO_AUTH=true (local testing
# only; loudly logged).
_ENABLE_AUTH_RAW = os.getenv("ENABLE_AUTH")
_ALLOW_INSECURE_NO_AUTH = os.getenv("ALLOW_INSECURE_NO_AUTH", "false").lower() == "true"


def _resolve_enable_auth(raw: Optional[str], allow_insecure: bool) -> bool:
    if raw is not None:
        normalized = raw.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    if allow_insecure:
        logger.warning(
            f"ENABLE_AUTH={raw!r} is unset or not exactly 'true'/'false'; "
            f"ALLOW_INSECURE_NO_AUTH=true is set, so this server is "
            f"starting WITHOUT OAuth authentication. Every request reaching "
            f"/mcp will be treated as authenticated. This must never be "
            f"used for a network-exposed deployment."
        )
        return False
    raise RuntimeError(
        f"ENABLE_AUTH={raw!r} is unset or ambiguous (expected exactly "
        f"'true' or 'false'). Refusing to start with authentication "
        f"implicitly disabled — a forgotten env var must not silently "
        f"expose every tool unauthenticated. Set ENABLE_AUTH=true, set it "
        f"to exactly 'false', or set ALLOW_INSECURE_NO_AUTH=true to "
        f"explicitly opt into running without auth (local testing only)."
    )


ENABLE_AUTH = _resolve_enable_auth(_ENABLE_AUTH_RAW, _ALLOW_INSECURE_NO_AUTH)


def _check_per_user_token_preconditions(
    per_user_token_mode: bool, enable_auth: bool, stateless_http: bool
) -> None:
    if not per_user_token_mode:
        return
    if not enable_auth:
        raise RuntimeError(
            "GWS_PER_USER_TOKEN=true requires ENABLE_AUTH=true — per-user "
            "credential isolation depends on a validated caller identity; "
            "running it without auth would let any unauthenticated request "
            "inject an arbitrary Google token."
        )
    if not stateless_http:
        # USER_TOKEN isolation relies on stateless_http=True: each request is
        # handled by a fresh task copied from the request's own context, so
        # the ContextVar set by the middleware is never visible to another
        # caller. Without it, the mcp SDK's stateful path can run tool calls
        # in a long-lived session task whose context was captured at
        # session-creation time — a later caller reusing that session would
        # silently execute with the session creator's token instead of
        # failing closed. Keep this coupled to server.py's
        # FastMCP(..., stateless_http=True); if that ever changes, this must
        # be revisited before GWS_PER_USER_TOKEN can be used safely.
        raise RuntimeError(
            "GWS_PER_USER_TOKEN=true requires the FastMCP server to be "
            "running with stateless_http=True (see server.py) — without "
            "it, per-user token isolation is not guaranteed across "
            "requests sharing a session."
        )


_check_per_user_token_preconditions(
    PER_USER_TOKEN_MODE, ENABLE_AUTH, mcp.settings.stateless_http
)

# --- REQUIRED_SCOPES: refuse to silently disable the scope check ---
#
# Leaving REQUIRED_SCOPES unset keeps the documented default scopes.
# Explicitly setting it to an empty string used to silently disable the
# scope check (any validly-signed token would connect) — treat that as a
# misconfiguration when auth is on, not a valid way to say "no scopes
# required"; require REQUIRE_SCOPES=false as an explicit opt-out instead.
_REQUIRED_SCOPES_RAW = os.getenv("REQUIRED_SCOPES")
_REQUIRE_SCOPES_OPT_OUT = os.getenv("REQUIRE_SCOPES", "true").lower() == "false"


def _resolve_required_scopes(
    raw: Optional[str], enable_auth: bool, opt_out: bool
) -> list:
    if raw is None:
        return ["gws:read", "gws:write"]
    scopes = raw.split()
    if not scopes and enable_auth and not opt_out:
        raise RuntimeError(
            "REQUIRED_SCOPES is set to an empty string, which would disable "
            "the OAuth scope check entirely — any validly-signed token "
            "would connect regardless of scope. Refusing to start. Set "
            "REQUIRED_SCOPES to a non-empty space-separated scope list, or "
            "set REQUIRE_SCOPES=false to explicitly opt out of scope "
            "enforcement."
        )
    return scopes


REQUIRED_SCOPES = _resolve_required_scopes(
    _REQUIRED_SCOPES_RAW, ENABLE_AUTH, _REQUIRE_SCOPES_OPT_OUT
)

# --- Per-request user token extraction (GWS_PER_USER_TOKEN) ---
#
# Exactly one source is consulted, chosen by which env var is set:
#   GWS_USER_TOKEN_CLAIM  — read the token out of this claim in the already-
#                           validated JWT payload (e.g. a Google access token
#                           embedded by a token-exchange IdP). Takes priority
#                           if set.
#   GWS_USER_TOKEN_HEADER — otherwise, read the token from this forwarded
#                           HTTP header (default "X-GWS-User-Token"),
#                           populated by the client with its own Google
#                           token.
# Both are only consulted when GWS_PER_USER_TOKEN=true (see server.py); the
# extracted value is stashed into the USER_TOKEN ContextVar for execute_gws.
GWS_USER_TOKEN_CLAIM = os.getenv("GWS_USER_TOKEN_CLAIM", "").strip()
GWS_USER_TOKEN_HEADER = os.getenv("GWS_USER_TOKEN_HEADER", "X-GWS-User-Token").strip()
_GWS_USER_TOKEN_HEADER_BYTES = GWS_USER_TOKEN_HEADER.lower().encode("latin-1")

if not PER_USER_TOKEN_MODE and (GWS_USER_TOKEN_CLAIM or os.getenv("GWS_USER_TOKEN_HEADER")):
    # Configuring a token source without GWS_PER_USER_TOKEN=true is a no-op —
    # nothing ever reads GWS_USER_TOKEN_CLAIM/HEADER unless per-user mode is
    # on — but it looks configured to an operator, so warn loudly rather
    # than silently running in shared host-identity mode.
    logger.warning(
        "GWS_USER_TOKEN_CLAIM or GWS_USER_TOKEN_HEADER is set, but "
        "GWS_PER_USER_TOKEN is not 'true' — this configuration has no "
        "effect and every request will still execute as the host's shared "
        "Google identity. Set GWS_PER_USER_TOKEN=true to enable per-user "
        "credential isolation."
    )

# --- Unauthenticated route allowlist ---
#
# /docs and /openapi.json are disabled by default (EXPOSE_API_DOCS=false):
# an unauthenticated Swagger UI/schema on a network-exposed server is
# unnecessary information-disclosure surface. Set EXPOSE_API_DOCS=true to
# restore them (e.g. for local development convenience).
EXPOSE_API_DOCS = os.getenv("EXPOSE_API_DOCS", "false").lower() == "true"


def _build_unauthenticated_paths(expose_docs: bool) -> set:
    paths = {"/health"}
    if expose_docs:
        # /docs/oauth2-redirect is FastAPI's default
        # swagger_ui_oauth2_redirect_url — Swagger UI's "Authorize" flow
        # redirects the browser there with no Bearer header, so it needs
        # the same bypass as /docs itself.
        paths.update({"/docs", "/openapi.json", "/docs/oauth2-redirect"})
    return paths


_UNAUTHENTICATED_PATHS = _build_unauthenticated_paths(EXPOSE_API_DOCS)

jwks_client = None

def get_jwks_client():
    global jwks_client
    if jwks_client is None:
        jwks_client = PyJWKClient(JWKS_URI)
    return jwks_client

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting GWS Remote MCP Server")
    # The streamable HTTP transport's session manager owns a background task
    # group that must be running before any /mcp request arrives, or every
    # request fails with "Task group is not initialized". Mounting the
    # sub-app alone does not start it — its own lifespan never runs when
    # mounted into a parent ASGI app, so it must be entered here explicitly.
    async with mcp.session_manager.run():
        yield
    logger.info("Shutting down GWS Remote MCP Server")

# Create the FastAPI app
app = FastAPI(
    title="GWS Remote MCP Server",
    description="An OAuth 2.1 protected MCP server wrapping the Google Workspace CLI",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if EXPOSE_API_DOCS else None,
    openapi_url="/openapi.json" if EXPOSE_API_DOCS else None,
    redoc_url=None,
)

def _extract_token_scopes(payload: dict) -> list:
    """Reads the JWT's granted scopes, accepting either the standard
    space-delimited "scope" string claim, or an Okta-style "scp" array claim
    — Okta (an IdP this server's docs explicitly support) commonly issues
    the latter instead of the former.
    """
    scope_claim = payload.get("scope")
    if isinstance(scope_claim, str):
        return scope_claim.split()
    scp_claim = payload.get("scp")
    return scp_claim if isinstance(scp_claim, list) else []


class ASGIAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope["path"]
        if path in _UNAUTHENTICATED_PATHS or not ENABLE_AUTH:
            return await self.app(scope, receive, send)

        # Check auth
        headers = dict(scope.get("headers", []))
        # errors="replace": HTTP field values may legally contain obs-text
        # bytes (RFC 7230) that aren't valid UTF-8 — a strict decode would
        # raise UnicodeDecodeError here and produce an unhandled 500 instead
        # of the intended 401 for what is just an invalid/malformed token.
        auth_header = headers.get(b"authorization", b"").decode("utf-8", errors="replace")

        if not auth_header.startswith("Bearer "):
            await self._send_401(send, "Missing or invalid Authorization header")
            return

        try:
            token = auth_header.split(" ")[1]
            client = get_jwks_client()
            # PyJWKClient does a synchronous HTTP fetch (cached, but still
            # blocking on a cache miss); run it off the event loop so a slow
            # or unreachable IdP doesn't stall every other in-flight request
            # (including long-lived SSE streams) on this worker.
            signing_key = await anyio.to_thread.run_sync(
                client.get_signing_key_from_jwt, token
            )
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=OAUTH_AUDIENCE,
                issuer=OAUTH_ISSUER,
                options={"require": ["exp"]},
            )

            # Check scopes (basic enterprise security practice).
            token_scopes = _extract_token_scopes(payload)
            if REQUIRED_SCOPES and not any(scope in token_scopes for scope in REQUIRED_SCOPES):
                await self._send_401(send, "Insufficient scope", 'Bearer error="insufficient_scope"')
                return

        except Exception as e:
            # Log the detail server-side only — echoing str(e) to the client
            # can leak internals (e.g. PyJWKClient connection errors embed
            # the configured JWKS URI and underlying network error text).
            logger.warning(f"Rejecting request with invalid token: {e}")
            await self._send_401(send, "Invalid token", 'Bearer error="invalid_token"')
            return

        # Per-request credential isolation: stash the caller's own Google
        # token into a ContextVar (never os.environ / a module global —
        # those would race across concurrent requests on this worker) so
        # execute_gws can inject it into just that one gws child process.
        if PER_USER_TOKEN_MODE:
            USER_TOKEN.set(self._extract_user_token(headers, payload))

        return await self.app(scope, receive, send)

    @staticmethod
    def _extract_user_token(headers: Dict[bytes, bytes], payload: dict) -> Optional[str]:
        if GWS_USER_TOKEN_CLAIM:
            claim_value = payload.get(GWS_USER_TOKEN_CLAIM)
            if isinstance(claim_value, str) and claim_value.strip():
                return claim_value.strip()
            return None
        raw = headers.get(_GWS_USER_TOKEN_HEADER_BYTES, b"").decode("utf-8", errors="replace").strip()
        return raw or None

    async def _send_401(self, send, detail, www_auth="Bearer"):
        import json
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", www_auth.encode("utf-8")),
                (b"content-length", str(len(body)).encode("utf-8"))
            ]
        })
        await send({
            "type": "http.response.body",
            "body": body
        })

app_with_auth = ASGIAuthMiddleware(app)

# Enterprise Health Check endpoint
@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "gws-mcp-server"}

mcp_starlette = mcp.streamable_http_app()
# FastMCP's streamable_http_app() already serves at its configured
# streamable_http_path (default "/mcp"), so mount it at the root — mounting
# it at "/mcp" here would nest it as "/mcp/mcp" and break the documented
# POST /mcp endpoint.
app.mount("/", mcp_starlette)

if __name__ == "__main__":
    import uvicorn
    # When running directly. Note this only covers the dev-mode `python
    # app.py` path — the documented production commands (uvicorn/gunicorn
    # CLI invocations, the Dockerfile CMD) bind an explicit port and must be
    # updated to match if you change the default here.
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app_with_auth", host="0.0.0.0", port=port, reload=True)
