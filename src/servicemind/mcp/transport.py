"""Compatibility import for the MCP provider adapter.

The implementation lives with the tool platform so the MCP server can depend on
tool contracts without creating a package cycle back from tool_platform to mcp.
"""

from servicemind.tool_platform.mcp_transport import (
    MCP_PROTOCOL_VERSION,
    TASK_EXTENSION,
    McpGlpiProvider,
    StatelessMcpClient,
)

__all__ = ["MCP_PROTOCOL_VERSION", "TASK_EXTENSION", "McpGlpiProvider", "StatelessMcpClient"]
