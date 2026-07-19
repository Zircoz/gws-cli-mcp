import asyncio
import contextvars
import json
import logging
import os
from typing import Any, Dict, List, Optional, Set
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Initialize FastMCP server. stateless_http=True avoids server-side session
# state: without it, sessions live in an in-memory dict on whichever worker
# process handled `initialize`, so a multi-worker deployment (e.g. gunicorn
# -w 4, as documented for production) would 404 any follow-up request routed
# to a different worker.
mcp = FastMCP("GWS Remote MCP Server", stateless_http=True)

# Service/meta-command names that are never reachable through execute_gws,
# regardless of GWS_ALLOWED_SERVICES. GWS_ALLOWED_SERVICES is a Workspace
# *service* allowlist (drive, gmail, ...); it was never meant to gate gws's
# own CLI meta-commands. In particular "auth" can print decrypted OAuth
# credentials (`gws auth export --unmasked`) or destroy them (`gws auth
# logout`), so it must be blocked even when GWS_ALLOWED_SERVICES=*.
ALWAYS_BLOCKED_SERVICES: Set[str] = {"auth", "schema", "generate-skills"}

# Alias -> canonical Discovery API name, mirrored from the SERVICES table in
# crates/google-workspace/src/services.rs. Keep this in sync whenever a
# service or alias is added/removed there. It exists so the
# GWS_ALLOWED_SERVICES allowlist check treats an aliased service (e.g.
# "reports") identically to its canonical/other-aliased form ("admin" /
# "admin-reports") — without it, an operator who allowed one spelling could
# be silently bypassed by a caller using the other.
SERVICE_ALIASES: Dict[str, str] = {
    "drive": "drive",
    "sheets": "sheets",
    "gmail": "gmail",
    "calendar": "calendar",
    "admin-reports": "admin",
    "reports": "admin",
    "docs": "docs",
    "slides": "slides",
    "tasks": "tasks",
    "people": "people",
    "chat": "chat",
    "classroom": "classroom",
    "forms": "forms",
    "keep": "keep",
    "meet": "meet",
    "events": "workspaceevents",
    "modelarmor": "modelarmor",
    "workflow": "workflow",
    "wf": "workflow",
    "script": "script",
}


def _normalize_service(name: str) -> str:
    """Maps a service alias to its canonical Discovery API name.

    Names with no known alias pass through unchanged: they either resolve to
    nothing in `gws` itself (rejected there as "Unknown service") or are
    compared to the allowlist as-is.
    """
    return SERVICE_ALIASES.get(name, name)


# --- Scope / service restriction ---
#
# GWS_ALLOWED_SERVICES: comma-separated list of gws service names that clients
# may invoke through this MCP server (e.g. "drive,gmail,calendar").
# Omit the variable (or set it to "*") to allow all services — including,
# via `gws`'s "service:version" Discovery syntax, non-Workspace Google APIs
# reachable by the host credential. execute_gws blocks that syntax outright
# (see below), so "*" here still only ever reaches gws's known service list.
#
# Examples:
#   GWS_ALLOWED_SERVICES=drive,gmail         # read/write Drive + Gmail only
#   GWS_ALLOWED_SERVICES=drive,gmail,calendar,sheets
#   GWS_ALLOWED_SERVICES=*                   # unrestricted (default)
_ALLOWED_SERVICES_ENV: str = os.getenv("GWS_ALLOWED_SERVICES", "").strip()
ALLOWED_SERVICES: Optional[Set[str]] = (
    {s.strip() for s in _ALLOWED_SERVICES_ENV.split(",") if s.strip()}
    if _ALLOWED_SERVICES_ENV and _ALLOWED_SERVICES_ENV != "*"
    else None  # None = no restriction
)
# Canonicalized once at import time so the per-request check in execute_gws
# is a plain set-membership test regardless of which alias the operator used
# in GWS_ALLOWED_SERVICES.
ALLOWED_SERVICES_CANONICAL: Optional[Set[str]] = (
    {_normalize_service(s) for s in ALLOWED_SERVICES}
    if ALLOWED_SERVICES is not None
    else None
)

if ALLOWED_SERVICES is not None:
    logger.info(f"Service allowlist active: {sorted(ALLOWED_SERVICES)}")
else:
    logger.info(
        "Service allowlist: unrestricted (GWS_ALLOWED_SERVICES not set or "
        "'*'). This exposes every Google Discovery API reachable by the "
        "host credential's scopes, not just Workspace services — set "
        "GWS_ALLOWED_SERVICES to confine the surface."
    )

# --- Per-request (multi-tenant) credential isolation ---
#
# By default, this server executes every request as the single Google
# identity configured on the host process (via GOOGLE_WORKSPACE_CLI_TOKEN /
# GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE / ADC). That's fine for single-tenant
# deployments, but FastMCP(..., stateless_http=True) means one worker process
# serves many concurrent callers — with only host-identity credentials, every
# caller acts as the same Google account.
#
# GWS_PER_USER_TOKEN=true switches to per-request mode: the caller's own
# Google access token — extracted per-request by ASGIAuthMiddleware in
# app.py, after JWT validation — is injected into just that one `gws` child
# process's environment, so each call executes as the calling user instead
# of the host.
#
def _resolve_per_user_token_mode(raw: Optional[str]) -> bool:
    """Parses GWS_PER_USER_TOKEN strictly (must be exactly "true"/"false"/
    unset), not with a lenient `.lower() == "true"` comparison: unlike the
    other boolean env vars here, an ambiguous value for this one fails
    toward *less* security (silently falling back to shared host-identity
    mode) rather than more, so it gets the same fail-closed treatment as
    ENABLE_AUTH in app.py.
    """
    if raw is None or raw.strip() == "":
        return False
    normalized = raw.strip().lower()
    if normalized not in ("true", "false"):
        raise RuntimeError(
            f"GWS_PER_USER_TOKEN={raw!r} is not a recognized value. Set it "
            f"to exactly 'true' or 'false' — ambiguous values (e.g. '1', "
            f"'yes') are rejected rather than silently falling back to "
            f"shared host-identity credentials."
        )
    return normalized == "true"


PER_USER_TOKEN_MODE: bool = _resolve_per_user_token_mode(os.getenv("GWS_PER_USER_TOKEN"))

# Set per-request by app.py's ASGIAuthMiddleware. A contextvars.ContextVar is
# required here rather than a module-level global/dict: the stateless_http
# transport serves concurrent requests from multiple clients on the same
# worker process, and a plain global would let one caller's token leak into
# another caller's concurrent request. ContextVar values are scoped to the
# asyncio task (and any children created from it) that set them.
USER_TOKEN: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "gws_user_token", default=None
)


@mcp.tool()
async def execute_gws(
    service: str,
    command: str,
    params: Optional[Dict[str, Any]] = None,
    args: Optional[List[str]] = None,
) -> str:
    """
    Execute a gws (Google Workspace CLI) command on the server.

    :param service: The gws service name (e.g. "drive", "gmail", "calendar").
    :param command: Sub-command string to pass after the service name (e.g. "files list").
    :param params: Optional JSON object forwarded as --params.
    :param args:   Optional list of additional raw CLI arguments.
    """
    # Enforce service restrictions before touching the subprocess, in order:
    # 1. Meta-commands are blocked unconditionally, regardless of allowlist.
    # 2. The "service:version" escape hatch (reaches arbitrary Discovery
    #    APIs) is closed outright.
    # 3. The "--api-version" flag escape hatch is closed outright — gws
    #    applies it while keeping the service's canonical api_name, so it
    #    can silently repoint an allowed service (e.g. "reports") at a
    #    completely different Google API sharing that api_name (e.g.
    #    "admin"'s "directory_v1" instead of "reports_v1") without ever
    #    touching the `service` argument the allowlist checks.
    # 4. Aliases are normalized to their canonical name.
    # 5. Only then is the (canonicalized) allowlist consulted.
    if service in ALWAYS_BLOCKED_SERVICES:
        return (
            f"Error: Service '{service}' is a CLI meta-command and is never "
            f"reachable through execute_gws."
        )

    if ":" in service:
        return (
            f"Error: Service '{service}' uses the 'api:version' Discovery "
            f"syntax, which is not permitted through execute_gws. That "
            f"syntax lets gws reach arbitrary Google Discovery APIs "
            f"(including non-Workspace ones) regardless of "
            f"GWS_ALLOWED_SERVICES. Use a plain service name instead (e.g. "
            f"'drive', not 'drive:v3')."
        )

    for token in [*command.split(), *(args or [])]:
        if token == "--api-version" or token.startswith("--api-version="):
            return (
                "Error: '--api-version' is not permitted through "
                "execute_gws. It overrides the Discovery API version while "
                "keeping the same service name, which can silently repoint "
                "an allowed service at a different, unintended Google API."
            )

    canonical_service = _normalize_service(service)

    if (
        ALLOWED_SERVICES_CANONICAL is not None
        and canonical_service not in ALLOWED_SERVICES_CANONICAL
    ):
        allowed_str = ", ".join(sorted(ALLOWED_SERVICES))
        return (
            f"Error: Service '{service}' is not permitted on this server.\n"
            f"Allowed services: {allowed_str}"
        )

    cmd = ["gws", service]
    cmd.extend(command.split())

    if params:
        cmd.extend(["--params", json.dumps(params)])

    if args:
        cmd.extend(args)

    # Build the child's environment explicitly instead of relying on
    # implicit inheritance, and never mutate os.environ itself — mutating it
    # would race across concurrent requests handled by this same worker.
    base_env = os.environ.copy()
    if PER_USER_TOKEN_MODE:
        token = USER_TOKEN.get()
        if not token:
            return (
                "Error: GWS_PER_USER_TOKEN is enabled but no per-user token "
                "was provided for this request. Refusing to fall back to "
                "the host's Google credentials."
            )
        # Request-scoped: this dict is local to this call and passed only to
        # this one child process, so it is never shared/global state.
        child_env = {**base_env, "GOOGLE_WORKSPACE_CLI_TOKEN": token}
    else:
        child_env = base_env

    try:
        # Log only the service and the resource+verb pair — never the full
        # command, --params, or args, which can carry request PII (message
        # bodies, search queries, resource IDs). gws's own CLI surface never
        # needs more than two tokens here: every Discovery-driven method
        # takes its parameters via --params/--json (never positionally), and
        # the "+verb" helpers take a single token. Anything a caller appends
        # beyond that is either noise or exactly the kind of embedded
        # identifier this redaction exists to keep out of the logs.
        logged_command = " ".join(command.split()[:2])
        logger.info(f"Executing gws command: service={service!r} command={logged_command!r}")
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )

        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            return f"Error (Exit Code {process.returncode}):\n{stderr.decode()}"

        return stdout.decode()

    except FileNotFoundError:
        return (
            "Error: 'gws' binary not found. "
            "Install it and ensure it is on PATH before starting this server."
        )
    except Exception as e:
        return f"Failed to execute command: {str(e)}"
