"""Protocol checks against real, separately started MCP server processes."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from vista_memory import VistaStore

mcp = pytest.importorskip("mcp")


def _png(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 3), color).save(output, format="PNG")
    return output.getvalue()


def _client(root: Path) -> mcp.Client:
    return mcp.Client(
        mcp.StdioServerParameters(
            command=sys.executable,
            args=["-m", "vista_memory.mcp", "--root", str(root)],
        ),
        read_timeout_seconds=10,
    )


def _assert_record(result, metadata: dict, png: bytes, code: str | None, mode: str):
    assert not result.is_error
    assert result.structured_content is None
    blocks = result.content
    expected_types = ["text"]
    if mode in {"code", "both"}:
        expected_types.append("text")
    if mode in {"image", "both"}:
        expected_types.append("image")
    assert [block.type for block in blocks] == expected_types
    assert json.loads(blocks[0].text) == metadata
    if mode in {"code", "both"}:
        assert blocks[1].text == (code if code is not None else "没有关联代码")
    if mode in {"image", "both"}:
        assert blocks[-1].mime_type == "image/png"
        assert base64.b64decode(blocks[-1].data, validate=True) == png


def test_stdio_discovery_schema_and_read_modes(tmp_path: Path):
    store = VistaStore(tmp_path)
    png = _png((18, 50, 92))
    code = "// 完整代码\nvoid main() {\n    // keep all whitespace\n}\n"
    render = {"time": 0, "uniforms": {"radius": 0.2, "tint": [0.1, 0.2, 0.3]}}
    rendered = store.save_record(
        png=png,
        glsl=code,
        label="第 1 次渲染",
        source_agent="generation",
        render=render,
    )
    reference = store.save_record(png=png, label="参考图")
    empty_code = store.save_record(png=png, label="空代码", glsl="")

    async def scenario():
        async with _client(tmp_path) as client:
            assert client.protocol_version
            assert client.server_info.name == "vista-memory"
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert set(tools) == {"list_records", "read_record"}
            list_schema = tools["list_records"].input_schema
            assert list_schema["type"] == "object"
            assert list_schema.get("properties", {}) == {}
            assert list_schema.get("required", []) == []
            read_schema = tools["read_record"].input_schema
            assert read_schema["type"] == "object"
            assert set(read_schema["properties"]) == {"record", "content"}
            assert read_schema["properties"]["record"]["type"] == "string"
            assert read_schema["required"] == ["record"]
            assert read_schema["properties"]["content"]["default"] == "image"
            assert read_schema["properties"]["content"]["enum"] == [
                "image",
                "code",
                "both",
            ]

            catalog = await client.call_tool("list_records", {})
            assert not catalog.is_error
            assert catalog.structured_content is None
            assert [block.type for block in catalog.content] == ["text"]
            assert json.loads(catalog.content[0].text) == store.list_records()
            for summary, expected_code in [
                (rendered, code),
                (reference, None),
                (empty_code, ""),
            ]:
                metadata = {
                    "record": summary["record"],
                    "label": summary["label"],
                    "source_agent": summary["source_agent"],
                }
                if summary == rendered:
                    metadata["render"] = render
                for mode in [None, "image", "code", "both"]:
                    arguments = {"record": summary["record"]}
                    if mode is not None:
                        arguments["content"] = mode
                    result = await client.call_tool("read_record", arguments)
                    _assert_record(
                        result, metadata, png, expected_code, mode or "image"
                    )

    asyncio.run(scenario())


def test_stdio_discovers_live_appends_and_restarts(tmp_path: Path):
    store = VistaStore(tmp_path)
    first_png = _png((255, 0, 0))
    second_png = _png((0, 0, 255))
    code = "void main() {}\n"
    first = store.save_record(
        png=first_png, glsl=code, label="first", render={"radius": 0.2}
    )

    async def scenario():
        async with _client(tmp_path) as client:
            before = await client.call_tool("list_records", {})
            assert json.loads(before.content[0].text) == {"records": [first]}
            second = store.save_record(
                png=second_png,
                reuse_glsl_from=first["record"],
                label="second",
                render={"radius": 0.3},
            )
            # The returned summary is enough to read a new record immediately.
            result = await client.call_tool(
                "read_record", {"record": second["record"], "content": "both"}
            )
            _assert_record(
                result,
                {
                    "record": second["record"],
                    "label": "second",
                    "source_agent": None,
                    "render": {"radius": 0.3},
                },
                second_png,
                code,
                "both",
            )
            after = await client.call_tool("list_records", {})
            assert not after.is_error
            assert json.loads(after.content[0].text) == store.list_records()

        # A new Client starts a new server process, with no retained session state.
        async with _client(tmp_path) as client:
            catalog = await client.call_tool("list_records", {})
            assert not catalog.is_error
            assert json.loads(catalog.content[0].text) == store.list_records()
            for summary, png, radius in [
                (first, first_png, 0.2),
                (second, second_png, 0.3),
            ]:
                result = await client.call_tool(
                    "read_record", {"record": summary["record"], "content": "both"}
                )
                _assert_record(
                    result,
                    {
                        "record": summary["record"],
                        "label": summary["label"],
                        "source_agent": None,
                        "render": {"radius": radius},
                    },
                    png,
                    code,
                    "both",
                )

    asyncio.run(scenario())


def test_stdio_tool_errors_and_recovery(tmp_path: Path):
    store = VistaStore(tmp_path)
    saved = store.save_record(png=_png((50, 60, 70)), label="recoverable")

    async def scenario():
        async with _client(tmp_path) as client:
            for arguments in [
                {},
                {"record": saved["record"], "content": "invalid"},
                {"record": 42},
                {"record": "missing.json"},
                {"record": "../outside.json"},
            ]:
                failed = await client.call_tool("read_record", arguments)
                assert failed.is_error, arguments
                assert failed.content
                assert all(block.type == "text" for block in failed.content)
                assert any(block.text for block in failed.content)

            record_path = tmp_path / "json" / saved["record"]
            record_json = json.loads(record_path.read_text(encoding="utf-8"))
            png_path = tmp_path / record_json["png"]
            png = png_path.read_bytes()
            png_path.unlink()
            missing = await client.call_tool("read_record", {"record": saved["record"]})
            assert missing.is_error
            assert saved["record"] in missing.content[0].text
            assert Path(record_json["png"]).name in missing.content[0].text
            png_path.write_bytes(png)

            broken_path = tmp_path / "json" / "broken.json"
            broken_path.write_text("{", encoding="utf-8")
            broken = await client.call_tool("list_records", {})
            assert broken.is_error
            assert "broken.json" in broken.content[0].text
            broken_path.unlink()
            recovered = await client.call_tool("list_records", {})
            assert not recovered.is_error
            assert json.loads(recovered.content[0].text) == {"records": [saved]}

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["missing_root", "root_is_file"])
def test_cli_errors_use_stderr(tmp_path: Path, failure: str):
    args = [sys.executable, "-m", "vista_memory.mcp"]
    if failure == "root_is_file":
        root = tmp_path / "not-a-directory"
        root.write_text("occupied", encoding="utf-8")
        args.extend(["--root", str(root)])
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "error:" in result.stderr
    assert "Traceback" not in result.stderr
    if failure == "missing_root":
        assert "--root" in result.stderr
    else:
        assert str(root) in result.stderr
