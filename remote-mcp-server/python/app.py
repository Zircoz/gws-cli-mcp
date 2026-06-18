import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from starlette.responses import JSONResponse
import jwt
from jwt import PyJWKClient

from server import mcp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gws-mcp-server")

OAUTH_ISSUER = os.getenv("OAUTH_ISSUER", "https://your-idp.example.com/")
OAUTH_AUDIENCE = os.getenv("OAUTH_AUDIENCE", "https://mcp.example.com")
JWKS_URI = os.getenv("JWKS_URI", f"{OAUTH_ISSUER}.well-known/jwks.json")
ENABLE_AUTH = os.getenv("ENABLE_AUTH", "false").lower() == "true"
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
    yield
    logger.info("Shutting down GWS Remote MCP Server")

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

@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "gws-mcp-server"}

from mcp.server.sse import SseServerTransport
transport = SseServerTransport("/mcp/messages")

@app.get("/mcp/sse")
async def handle_sse(request: Request):
    async with transport.connect_sse(request.scope, request.receive, request._send) as streams:
        await mcp._mcp_server.run(streams[0], streams[1], mcp._mcp_server.create_initialization_options())

@app.post("/mcp/messages")
async def handle_messages(request: Request):
    await transport.handle_post_message(request.scope, request.receive, request._send)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app_with_auth", host="0.0.0.0", port=8000, reload=True)
