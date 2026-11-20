"""Exercise the T05 retrieval and fallback paths locally before model acceptance."""

import asyncio
import base64
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import mcp
from dsh_sdk_run import save_report
from shader_recall_followup import PROJECT, checkpoint, original_config
from shader_trigger_validation import ACCEPTANCE_OUT, digest, version

# Reuse the wire auditor on a local DS-shaped message. No provider or model is called.
INSPECT = """
import { inspectBody, inspectTrigger } from './scripts/dsh_request_audit.mjs';
import { readFileSync } from 'node:fs';
const { content, expected } = JSON.parse(readFileSync(0,'utf8'));
const body = {messages:[{role:'user',content:[{type:'tool_result',
  tool_use_id:'offline-read', content}]}]};
const png = inspectBody(body,[],new Map(),[],true).images;
const matches = inspectTrigger(body,expected);
console.log(JSON.stringify({png:png.some(i=>i.sha256===expected.png &&
  i.toolCallId==='offline-read'),
  glsl:matches.some(r=>r.kinds.includes('full-target-glsl')),
  parameters:matches.some(r=>r.kinds.includes('target-render-parameters'))}));
"""


async def scenario(fixture, case, parent):
    library = parent / case
    shutil.copytree(fixture["vistaRoot"], library)
    inventory = fixture["baseTask"]["candidates"]
    selected = next(
        i for i, c in enumerate(inventory) if c["id"] == fixture["currentCandidateId"]
    )
    assert selected > 0
    earlier = inventory[selected - 1]
    label = f"candidate-{selected}-render"
    truth = fixture["mapping"][earlier["id"]]
    record_path = library / "json" / truth["record"]
    record = json.loads(record_path.read_bytes())
    if case == "missing-png":
        (library / record["png"]).unlink()
    elif case == "missing-record":
        record_path.unlink()
    elif case == "ambiguous-label":
        shutil.copyfile(
            record_path,
            record_path.with_name("record-00000000000000000000000000000000.json"),
        )

    operations = []
    reason = None
    client = mcp.Client(
        mcp.StdioServerParameters(
            command=sys.executable,
            args=["-m", "vista_memory.mcp", "--root", str(library)],
        ),
        read_timeout_seconds=10,
    )
    async with client:
        names = sorted(t.name for t in (await client.list_tools()).tools)
        assert names == ["list_records", "read_record"]
        catalog = await client.call_tool("list_records", {})
        assert not catalog.is_error
        rows = json.loads(catalog.content[0].text)["records"]
        matches = [r["record"] for r in rows if r["label"] == label and r["has_glsl"]]
        operations.append(
            {"operation": "list_records", "rows": len(rows), "matches": len(matches)}
        )
        if len(matches) == 1:
            result = await client.call_tool(
                "read_record", {"record": matches[0], "content": "both"}
            )
            operations.append(
                {
                    "operation": "read_record",
                    "record": matches[0],
                    "mode": "both",
                    "succeeded": not result.is_error,
                }
            )
            if result.is_error:
                reason = result.content[0].text
            else:
                meta, shader, image = result.content
                assert (meta.type, shader.type, image.type) == ("text", "text", "image")
                assert image.mime_type == "image/png"
                metadata = json.loads(meta.text)
                assert metadata["record"] == matches[0] == truth["record"]
                png = base64.b64decode(image.data, validate=True)
                glsl = shader.text
                render = metadata["render"]
                parameter_text = meta.text
        else:
            reason = (
                "no_matching_catalog_record"
                if not matches
                else "ambiguous_catalog_label"
            )

    if reason:
        folder = Path(fixture["businessDirectory"]) / "candidates" / earlier["id"]
        operations.append(
            {
                "operation": "read_business_candidate",
                "candidate": earlier["id"],
                "files": ["candidate.json", "shader.frag", "render.png"],
            }
        )
        parameter_text = (folder / "candidate.json").read_text()
        candidate = json.loads(parameter_text)
        assert candidate["id"] == earlier["id"]
        png = (folder / "render.png").read_bytes()
        glsl = (folder / "shader.frag").read_bytes().decode()
        render = {k: candidate[k] for k in ("width", "height", "time")}
        render["uniforms"] = {p["name"]: p["default"] for p in candidate["params"]}

    assert (case == "normal") == (reason is None)
    assert (
        digest(png) == earlier["imageHash"]
        and digest(glsl.encode()) == earlier["sourceHash"]
    )
    assert render == truth["render"]
    content = [
        {"type": "text", "text": parameter_text},
        {"type": "text", "text": glsl},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(png).decode(),
            },
        },
    ]
    expected = dict(
        png=earlier["imageHash"],
        glsl=earlier["sourceHash"],
        candidate=earlier["id"],
        code=glsl,
        render=truth["render"],
        record=truth["record"],
        policy="unused",
        feedback="unused",
    )
    audit = subprocess.run(
        ["node", "--input-type=module", "-e", INSPECT],
        cwd=PROJECT,
        input=json.dumps(dict(content=content, expected=expected)),
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(audit.stdout)
    assert all(payload.values()), payload
    return dict(
        case=case,
        route="business" if reason else "vista-mcp",
        operations=operations,
        fallback_reason=reason,
        candidate=earlier["id"],
        png_sha256=digest(png),
        glsl_sha256=digest(glsl.encode()),
        render=render,
        local_serialization=payload,
    )


async def main():
    fixture = checkpoint()
    revision = version(Path(original_config()["businessRoot"]))["head"]
    with tempfile.TemporaryDirectory(prefix="t05-recall-offline-") as tmp:
        rows = [
            await scenario(fixture, n, Path(tmp))
            for n in ("normal", "missing-png", "missing-record", "ambiguous-label")
        ]
    save_report(
        ACCEPTANCE_OUT / "offline.json",
        dict(revision=revision, passed=True, model_requests=0, cases=rows),
    )
    for r in rows:
        print(r["case"], r["route"], r["local_serialization"])


if __name__ == "__main__":
    asyncio.run(main())
