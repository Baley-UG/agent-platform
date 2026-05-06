"""Stdio entry point for local development with Claude Desktop / Claude Code.

Run with:
    python -m app.mcp_stdio

The streamable-HTTP transport mounted on /mcp is the production
surface; this module exists so a developer can attach a local MCP
client (Claude Desktop, Claude Code, MCP Inspector) without spinning
up the API container.

No auth here — this transport binds to local stdio, not the network.
"""

from app.core.logging import logger
from app.mcp_server import mcp_server


def main() -> None:
    if mcp_server is None:
        raise RuntimeError(
            "MCP package not installed. `uv pip install mcp` and try again."
        )
    logger.info("mcp_stdio_starting")
    mcp_server.run(transport="stdio")


if __name__ == "__main__":
    main()
