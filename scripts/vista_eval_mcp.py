"""Serve VISTA with a controlled historical-image condition and call budgets."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from mcp.types import CallToolResult, TextContent

from vista_memory.mcp import create_server

IMAGE_UNAVAILABLE = "Historical image unavailable in this condition."
LIMITS = {"list_records": 2, "read_record": 8}


class ConditionMiddleware:
    """Use the original handlers and change only their image content blocks."""

    def __init__(self, condition: str, audit_log: Path):
        self.condition = condition
        self.audit_log = audit_log
        self.counts: Counter[str] = Counter()
        audit_log.parent.mkdir(parents=True, exist_ok=True)

    async def __call__(self, context, call_next):
        if context.method != "tools/call":
            return await call_next(context)
        params = context.params or {}
        name = params.get("name", "")
        self.counts[name] += 1
        allowed = name in LIMITS and self.counts[name] <= LIMITS[name]
        started = time.monotonic()
        audit = {
            "time": datetime.now(UTC).isoformat(),
            "condition": self.condition,
            "name": name,
            "arguments": params.get("arguments", {}),
            "call_number": self.counts[name],
            "allowed": allowed,
        }
        try:
            if not allowed:
                message = (
                    f"Evaluation call budget exceeded for {name}: "
                    f"maximum {LIMITS.get(name, 0)} calls per run."
                )
                result = CallToolResult(
                    content=[TextContent(text=message)], is_error=True
                ).model_dump(by_alias=True, exclude_none=True)
            else:
                result = await call_next(context)
            # MCP middleware receives the result after protocol serialization.
            if not isinstance(result, dict) or "content" not in result:
                raise TypeError("VISTA returned an unexpected tool result type")
            images = [
                block for block in result["content"] if block.get("type") == "image"
            ]
            audit["source_images"] = [
                {
                    "mime_type": image["mimeType"],
                    "sha256": hashlib.sha256(
                        base64.b64decode(image["data"], validate=True)
                    ).hexdigest(),
                }
                for image in images
            ]
            if self.condition == "text":
                result = {
                    **result,
                    "content": [
                        {"type": "text", "text": IMAGE_UNAVAILABLE}
                        if block.get("type") == "image"
                        else block
                        for block in result["content"]
                    ],
                }
            audit["returned_image_count"] = sum(
                block.get("type") == "image" for block in result["content"]
            )
            audit["is_error"] = result.get("isError", False)
            return result
        except Exception as error:
            audit["exception_type"] = type(error).__name__
            raise
        finally:
            audit["duration_seconds"] = time.monotonic() - started
            with self.audit_log.open("a") as stream:
                stream.write(json.dumps(audit, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--condition", choices=("text", "visual"), required=True)
    parser.add_argument("--audit-log", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    server = create_server(args.root)
    server.middleware.append(ConditionMiddleware(args.condition, args.audit_log))
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
