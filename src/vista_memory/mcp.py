"""Read-only MCP tools for a caller-selected VISTA data directory."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
from pathlib import Path
from typing import Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ImageContent, TextContent

from .core import VistaError, VistaStore


def create_server(root: str | Path) -> MCPServer:
    """Create the two VISTA reading tools bound to ``root``."""
    store = VistaStore(root)
    server = MCPServer("vista-memory")

    @server.tool(structured_output=False)
    def list_records() -> list[TextContent]:
        """List saved images by record filename, label, source and code availability."""
        try:
            catalog = store.list_records()
        except VistaError as exc:
            raise ToolError(str(exc)) from exc
        return [TextContent(text=json.dumps(catalog, ensure_ascii=False))]

    @server.tool(structured_output=False)
    def read_record(
        record: str, content: Literal["image", "code", "both"] = "image"
    ) -> list[TextContent | ImageContent]:
        """Read a JSON record's metadata and requested image, GLSL code, or both."""
        try:
            result = store.read_record(record, content=content)
        except VistaError as exc:
            raise ToolError(str(exc)) from exc

        blocks: list[TextContent | ImageContent] = [
            TextContent(text=json.dumps(result.metadata, ensure_ascii=False))
        ]
        if content in {"code", "both"}:
            blocks.append(
                TextContent(
                    text=result.code if result.code is not None else "没有关联代码"
                )
            )
        if result.image is not None:
            blocks.append(
                ImageContent(
                    data=base64.b64encode(result.image).decode("ascii"),
                    mime_type="image/png",
                )
            )
        return blocks

    return server


def main(argv: list[str] | None = None) -> None:
    """Run the VISTA MCP server over stdio."""
    parser = argparse.ArgumentParser(description="Read VISTA records over MCP stdio.")
    parser.add_argument("--root", required=True, type=Path, help="VISTA data directory")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    try:
        server = create_server(args.root)
    except VistaError as exc:
        parser.error(str(exc))
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
