import asyncio
import json
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional, Set
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server
mcp = FastMCP("GWS Remote MCP Server")

# --- Scope / service restriction ---
#
# GWS_ALLOWED_SERVICES: comma-separated list of gws service names that clients
# may invoke through this MCP server (e.g. "drive,gmail,calendar").
# Omit the variable (or set it to "*") to allow all services.
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

if ALLOWED_SERVICES is not None:
    logging.info(f"Service allowlist active: {sorted(ALLOWED_SERVICES)}")
else:
    logging.info("Service allowlist: unrestricted (GWS_ALLOWED_SERVICES not set or '*')")


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
    # Enforce service allowlist before touching the subprocess.
    if ALLOWED_SERVICES is not None and service not in ALLOWED_SERVICES:
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

    try:
        logging.info(f"Executing: {' '.join(cmd)}")
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
