import asyncio
import json
import logging
from typing import Any, Dict, List, Optional
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server
mcp = FastMCP("GWS Remote MCP Server")

@mcp.tool()
async def execute_gws(service: str, command: str, params: Optional[Dict[str, Any]] = None, args: Optional[List[str]] = None) -> str:
    """
    Executes a gws (Google Workspace CLI) command.
    """
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
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            return f"Error (Exit Code {process.returncode}):\n{stderr.decode()}"

        return stdout.decode()

    except Exception as e:
        return f"Failed to execute command: {str(e)}"
