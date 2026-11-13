"""Verify live MCP discovery and persisted content with deterministic PNGs.

Pillow generates each image from its recorded parameters. The GLSL strings are
storage fixtures; this example does not execute shaders or assess their images.
Run with: uv run --extra mcp python examples/local_roundtrip.py --root /tmp/vista
Every successful run appends four records and verifies them in two MCP processes.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from mcp import Client, StdioServerParameters, types
from PIL import Image, ImageDraw

from vista_memory import VistaStore

GLSL_V1 = """// Storage fixture version 1; not executed by this example.
void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    fragColor = vec4(0.2, 0.6, 0.9, 1.0);
}
"""
GLSL_V2 = """// Storage fixture version 2; not executed by this example.
void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    fragColor = vec4(0.9, 0.4, 0.2, 1.0);
}
"""


@dataclass
class ExpectedRecord:
    summary: dict
    png: bytes
    code: str | None
    render: dict | None

    @property
    def name(self) -> str:
        return self.summary["record"]

    @property
    def metadata(self) -> dict:
        result = {key: self.summary[key] for key in ("record", "label", "source_agent")}
        if self.render is not None:
            result["render"] = self.render
        return result


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def parameters(radius: int, foreground: list[int]) -> dict:
    return {
        "width": 96,
        "height": 96,
        "center": [48, 48],
        "radius": radius,
        "foreground": foreground,
        "background": [16, 24, 40],
    }


def make_png(render: dict) -> bytes:
    """Use every supplied image parameter, without randomness or timestamps."""
    image = Image.new(
        "RGB", (render["width"], render["height"]), tuple(render["background"])
    )
    x, y = render["center"]
    radius = render["radius"]
    ImageDraw.Draw(image).ellipse(
        (x - radius, y - radius, x + radius, y + radius),
        fill=tuple(render["foreground"]),
    )
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def save_fixture(
    store: VistaStore,
    *,
    label: str,
    render: dict,
    code: str | None,
    reuse_from: str | None = None,
) -> ExpectedRecord:
    png = make_png(render)
    recorded_render = render if code is not None else None
    summary = store.save_record(
        png=png,
        label=label,
        glsl=code if reuse_from is None else None,
        reuse_glsl_from=reuse_from,
        source_agent="local_roundtrip",
        render=recorded_render,
    )
    require(
        summary
        == {
            "record": summary["record"],
            "label": label,
            "source_agent": "local_roundtrip",
            "has_glsl": code is not None,
        },
        f"Saved summary does not match the fixture: {label}",
    )
    return ExpectedRecord(summary, png, code, recorded_render)


@asynccontextmanager
async def connect(root: Path):
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "vista_memory.mcp", "--root", str(root)],
    )
    async with Client(server, read_timeout_seconds=15) as session:
        discovered = await session.list_tools()
        require(
            sorted(tool.name for tool in discovered.tools)
            == ["list_records", "read_record"],
            "The MCP server must expose exactly the two VISTA tools",
        )
        yield session


async def call(session: Client, name: str, arguments: dict):
    result = await session.call_tool(name, arguments)
    require(isinstance(result, types.CallToolResult), f"Unexpected result for {name}")
    require(not result.is_error, f"{name} failed: {result.content}")
    return result.content


async def catalog(session: Client) -> dict[str, dict]:
    blocks = await call(session, "list_records", {})
    require(
        len(blocks) == 1 and isinstance(blocks[0], types.TextContent),
        "list_records must return one JSON text block",
    )
    records = json.loads(blocks[0].text)["records"]
    by_name = {record["record"]: record for record in records}
    require(len(by_name) == len(records), "The catalog contains duplicate names")
    return by_name


async def verify_records(session: Client, expected: list[ExpectedRecord]) -> None:
    listed = await catalog(session)
    for record in expected:
        require(
            listed.get(record.name) == record.summary,
            f"Missing or mismatched catalog summary: {record.name}",
        )
        for mode in ("image", "code", "both"):
            arguments = {"record": record.name}
            if mode != "image":
                arguments["content"] = mode
            blocks = await call(session, "read_record", arguments)
            wants_code = mode in ("code", "both")
            wants_image = mode in ("image", "both")
            expected_types = ["text"]
            if wants_code:
                expected_types.append("text")
            if wants_image:
                expected_types.append("image")
            require(
                [block.type for block in blocks] == expected_types,
                f"Unexpected content blocks: {record.name} ({mode})",
            )
            require(
                json.loads(blocks[0].text) == record.metadata,
                f"Metadata mismatch: {record.name} ({mode})",
            )
            if wants_code:
                expected_code = (
                    record.code if record.code is not None else "没有关联代码"
                )
                require(
                    blocks[1].text == expected_code,
                    f"GLSL mismatch: {record.name} ({mode})",
                )
            if wants_image:
                image_block = blocks[-1]
                require(
                    image_block.mime_type == "image/png",
                    f"Unexpected image MIME type: {record.name}",
                )
                require(
                    base64.b64decode(image_block.data, validate=True) == record.png,
                    f"PNG byte mismatch: {record.name} ({mode})",
                )


async def run(root: Path) -> dict:
    store = VistaStore(root)
    expected = [
        save_fixture(
            store,
            label="Reference fixture",
            render=parameters(8, [200, 200, 200]),
            code=None,
        )
    ]
    async with connect(root) as session:
        before = await catalog(session)
        require(expected[0].name in before, "Initial reference record is missing")
        fixtures = [
            ("Image fixture 1", parameters(12, [44, 160, 232]), GLSL_V1),
            ("Image fixture 2", parameters(24, [44, 160, 232]), GLSL_V1),
            ("Image fixture 3", parameters(32, [232, 104, 44]), GLSL_V2),
        ]
        for index, (label, render, code) in enumerate(fixtures):
            record = save_fixture(
                store,
                label=label,
                render=render,
                code=code,
                reuse_from=expected[1].name if index == 1 else None,
            )
            require(
                record.name not in before, "Saving unexpectedly reused a record name"
            )
            expected.append(record)
            await verify_records(session, [record])
            before[record.name] = record.summary
        await verify_records(session, expected)

    # A new context starts a new CLI process, with no state from the first server.
    async with connect(root) as session:
        await verify_records(session, expected)

    return {
        "status": "passed",
        "records_added": len(expected),
        "mcp_processes": 2,
        "live_append_verified": True,
        "restart_readback_verified": True,
        "image_generator": "Pillow",
        "glsl_executed": False,
        "records": [record.name for record in expected],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="VISTA data directory")
    arguments = parser.parse_args()
    result = asyncio.run(run(arguments.root.expanduser().resolve()))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
