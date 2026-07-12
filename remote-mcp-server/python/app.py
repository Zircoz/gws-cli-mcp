import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse
import jwt
from jwt import PyJWKClient

# Configure logging before importing server — server.py logs at import time,
# and logging.info() implicitly calls basicConfig() at WARNING level on
# first use, which would silently make a later basicConfig() call here a
# no-op if it hasn't run yet.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gws-mcp-server")

# Import our FastMCP server instance
from server import mcp

# OAuth 2.1 Configuration
OAUTH_ISSUER = os.getenv("OAUTH_ISSUER", "https://your-idp.example.com/")
OAUTH_AUDIENCE = os.getenv("OAUTH_AUDIENCE", "https://mcp.example.com") # Canonical Server URI
JWKS_URI = os.getenv("JWKS_URI", f"{OAUTH_ISSUER}.well-known/jwks.json")
ENABLE_AUTH = os.getenv("ENABLE_AUTH", "false").lower() == "true" # Disabled by default for local testing
REQUIRED_SCOPES = os.getenv("REQUIRED_SCOPES", "gws:read gws:write").split()

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
    lifespan=lifespan
)

class ASGIAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope["path"]
        if path in ["/health", "/docs", "/openapi.json"] or not ENABLE_AUTH:
            return await self.app(scope, receive, send)

        # Check auth
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("utf-8")

        if not auth_header.startswith("Bearer "):
            await self._send_401(send, "Missing or invalid Authorization header")
            return

        try:
            token = auth_header.split(" ")[1]
            client = get_jwks_client()
            signing_key = client.get_signing_key_from_jwt(token)
            payload = jwt.decode(token, signing_key.key, algorithms=["RS256", "ES256"], audience=OAUTH_AUDIENCE, issuer=OAUTH_ISSUER)

            # Check scopes (basic enterprise security practice)
            token_scopes = payload.get("scope", "").split()
            if REQUIRED_SCOPES and not any(scope in token_scopes for scope in REQUIRED_SCOPES):
                await self._send_401(send, "Insufficient scope", 'Bearer error="insufficient_scope"')
                return

        except Exception as e:
            await self._send_401(send, f"Invalid token: {str(e)}", 'Bearer error="invalid_token"')
            return

        return await self.app(scope, receive, send)

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
    # When running directly
    uvicorn.run("app:app_with_auth", host="0.0.0.0", port=8000, reload=True)
