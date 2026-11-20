"""Freeze an icon catalog, then run one two-turn native DSH session per case."""

import argparse
import hashlib
import io
import json
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageDraw
from vista_memory import VistaStore

HERE = Path(__file__).resolve().parent
OUT = HERE.parent
REPO = OUT.parents[2]
sys.path.insert(0, str(REPO / "scripts"))
from dsh_sdk_run import DEFAULT_CLI, RpcProcess, prompt_images, save_report, timestamp

HISTORY = "这是「潮汐」图标的定稿，收到后请只回“收到”。"
SUMMARY = "「潮汐」图标已经定稿。"
QUESTIONS = {
    "a": "上次定稿的「潮汐」图标，缺口朝哪边？",
    "b": "这次定稿的「潮汐」图标是蓝色的。请用一句话确认颜色。",
}
TOOLS = ["mcp__vista__list_records", "mcp__vista__read_record"]


def freeze():
    root = OUT / "catalog"
    root.mkdir(exist_ok=False)
    store = VistaStore(root)
    rows = []
    for label, angle in [
        ("潮汐图标 · 初稿", 180),
        ("潮汐图标 · 定稿", 90),
        ("航标图标 · 定稿", 0),
    ]:
        im = Image.new("RGB", (256, 256), "white")
        ImageDraw.Draw(im).arc(
            (32, 32, 224, 224), angle + 31, angle + 329, (31, 94, 184), 28
        )
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        png = buf.getvalue()
        info = store.save_record(png=png, label=label, source_agent="design")
        materials = store.read_record(info["record"])
        assert materials.image == png
        if angle == 90:
            probes = {
                name: im.getpixel(point)
                for name, point in {
                    "top": (128, 45),
                    "bottom": (128, 211),
                    "left": (45, 128),
                    "right": (211, 128),
                }.items()
            }
            assert (
                probes["bottom"] == (255, 255, 255) and len(set(probes.values())) == 2
            )
            assert probes["left"] == probes["top"] == probes["right"]
        path = json.loads((root / "json" / info["record"]).read_text())["png"]
        rows.append({**info, "png": path, "sha256": hashlib.sha256(png).hexdigest()})
    save_report(
        OUT / "frozen.json",
        dict(
            created=timestamp(),
            records=rows,
            target=rows[1],
            direction="下",
            pixel_check={"radius": 83, "center": [128, 128], "rgb": probes},
            history=HISTORY,
            summary=SUMMARY,
            questions=QUESTIONS,
            allowed_tools=TOOLS,
            provider="deepseek-account",
            model="deepseek-flash",
            reasoning="high",
            max_output_tokens=4096,
            max_requests={"history": 3, "question": 8},
            seconds_per_turn=300,
            score={
                "a": "自主发起图片回读，选中目标定稿，紧接的成功模型请求中该工具调用携带匹配原 PNG 的哈希。视觉回答单独判。",
                "b": "根据当前文字确认蓝色，历史图片打开零次。目录与读取分别计数。",
            },
        ),
    )


def run(case, resume=False):
    frozen = json.loads((OUT / "frozen.json").read_text())
    target = frozen["target"]
    output = OUT / case
    if resume:
        old = json.loads((output / "run.json").read_text())
        audit = [
            json.loads(s) for s in (output / "wire.jsonl").read_text().splitlines()
        ]
        assert not any(row.get("phase") == "question" for row in audit)
        assert old["turns"][-1]["events"][-1]["data"]["reason"]["kind"] == "error"
        temp = Path(old["workspace"]).parent
        sid = old["session"]
        report = old
        if not (output / "before-recovery.json").exists():
            save_report(output / "before-recovery.json", old)
    else:
        output.mkdir(exist_ok=False)
        temp = Path(tempfile.mkdtemp(prefix="native-vista-")).resolve()
        sid = str(uuid4())
        report = dict(
            session=sid,
            workspace=str(temp / "work"),
            profile="sdk-minimal",
            started=timestamp(),
            turns=[],
        )
    home, cwd = temp / "home", temp / "work"
    home.mkdir(exist_ok=resume)
    cwd.mkdir(exist_ok=resume)
    marker = temp / "stage2"
    base = json.loads(
        (REPO / "docs/test-results/20261116-dsh/sdk-overlay.json").read_text()
    )
    inserts = base.pop()["insert"]
    entries = [
        entry for entry in inserts if entry["id"] in {"credentials-local", "mcp-vista"}
    ]
    entries[1]["config"]["args"] = ["--root", str(OUT / "catalog")]
    entries += [
        {"id": "account-auth", "name": "@deepseek-ai/dsh-authorization"},
        {
            "id": "account",
            "name": "@deepseek-ai/dsh-deepseek-account",
            "config": {"desktopPlatform": "darwin"},
        },
        {
            "id": "account-models",
            "name": "@deepseek-ai/dsh-llm-deepseek-account",
            "config": {"modelsMaxAgeMs": 600000},
        },
        {
            "id": "observer",
            "name": str(HERE / "observe.mjs"),
            "config": dict(
                marker=str(marker),
                log=str(output / "wire.jsonl"),
                question=QUESTIONS[case],
                summary=SUMMARY,
                digest=target["sha256"],
            ),
        },
    ]
    overlay = base + [
        {"id": "session-log-deepseek", "disabled": True},
        {"id": "plugin-package-inventory-deepseek", "disabled": True},
        {"insert": entries},
    ]
    save_report(output / "overlay.json", overlay)
    rpc = RpcProcess(
        [
            DEFAULT_CLI,
            "--profile",
            "sdk-minimal",
            "--patch",
            str(output / "overlay.json"),
        ],
        home,
        cwd,
    )
    try:
        report["initialize"] = rpc.request(
            "initialize",
            dict(
                cwd=str(cwd),
                provider=frozen["provider"],
                model=frozen["model"],
                reasoningEffort=frozen["reasoning"],
                maxTokens=frozen["max_output_tokens"],
            ),
            time.monotonic() + 90,
        )
        if resume:
            rpc.request("session/resume", dict(sessionId=sid), time.monotonic() + 60)
        for stage, text in [("history", HISTORY), ("question", QUESTIONS[case])]:
            if resume and stage == "history":
                continue
            blocks = [{"type": "text", "text": text}]
            if stage == "history":
                blocks += prompt_images(
                    [{"path": str(OUT / "catalog" / target["png"]), "name": "icon.png"}]
                )[0]
            else:
                marker.touch(exist_ok=resume)
            start = len(rpc.notifications)
            deadline = time.monotonic() + 300
            rpc.request(
                "session/prompt", dict(sessionId=sid, contentBlocks=blocks), deadline
            )
            rpc.request("session/wait", dict(sessionId=sid), deadline)
            events = [
                m["params"]["event"]
                for m in rpc.notifications[start:]
                if m.get("method") == "session.event"
                and m["params"]["sessionId"] == sid
            ]
            ends = [e for e in events if e["type"] == "turn/end"]
            report["turns"].append(dict(stage=stage, text=text, events=events))
            save_report(output / "run.json", report)
            if not ends or ends[-1]["data"]["reason"]["kind"] != "completed":
                raise RuntimeError(f"{stage} did not complete")
            print(f"{case} {stage} completed", flush=True)
        rpc.request("shutdown", {}, time.monotonic() + 25)
        rpc.process.wait(timeout=10)
    finally:
        rpc.close()
        report.update(finished=timestamp(), stderr=rpc.stderr)
        save_report(output / "run.json", report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["freeze", "a", "b", "resume-a"])
    cmd = parser.parse_args().action
    freeze() if cmd == "freeze" else run(
        cmd.removeprefix("resume-"), cmd.startswith("resume-")
    )
