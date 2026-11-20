"""Check VISTA delivery against native DSH with frozen synthetic pictures."""

from __future__ import annotations

import argparse
import hashlib
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from dsh_sdk_run import DEFAULT_CLI, RpcProcess, prompt_images, save_report
from PIL import Image, ImageDraw
from run_vista_history_experiment import MODEL, RUNTIME, overlay

from vista_memory import VistaStore
from vista_memory.history_catalog import HistoryCatalog
from vista_memory.history_store import TaskHistory

PROBE = Path(__file__).with_name("vista_history_dsh_probe.mjs")
TASK_ID = "vista-history-host-probe"
LABEL = "用户认可过这版留白"
RECEIPT = f"已存 R2 · {LABEL}"
IMPLICIT = "这一版的留白好像又大了。"
SIMPLE = "我们继续做同一组图。这一步只回复“收到”。"


def picture(path, margin, corner):
    image = Image.new("RGB", (256, 256), "#f9faf6")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (margin, margin, 255 - margin, 255 - margin), 19, fill="#365b83"
    )
    x, y = corner
    draw.ellipse((x - 7, y - 7, x + 7, y + 7), fill="#ffdd69")
    image.save(path)


def phase_prompt(rpc, sid, marker, name, text, image=None):
    marker.write_text(name)
    blocks = [{"type": "text", "text": text}]
    if image:
        blocks += prompt_images([{"path": str(image), "name": "current.png"}])[0]
    deadline = time.monotonic() + 180
    rpc.request("session/prompt", {"sessionId": sid, "contentBlocks": blocks}, deadline)
    rpc.request("session/wait", {"sessionId": sid}, deadline)
    events = [
        item["params"]["event"]
        for item in rpc.notifications
        if item["method"] == "session.event"
    ]
    end = next(event for event in reversed(events) if event["type"] == "turn/end")
    if end["data"]["reason"]["kind"] != "completed":
        raise RuntimeError(f"{name}: {end['data']['reason']}")


def compact(rpc, sid, marker):
    marker.write_text("native-compact")
    deadline = time.monotonic() + 180
    result = rpc.request(
        "session/query",
        {"sessionId": sid, "contentBlocks": [{"type": "text", "text": "/compact"}]},
        deadline,
    )
    if result["result"]["kind"] != "command":
        raise RuntimeError("DSH did not dispatch native /compact")
    while time.monotonic() < deadline:
        event = next(
            (
                row
                for row in rpc.notifications
                if row["method"] == "session.query.completed"
            ),
            None,
        )
        if event:
            if not any(
                row["params"]["event"]["type"] == "compaction/summary"
                for row in rpc.notifications
                if row["method"] == "session.event"
            ):
                raise RuntimeError(
                    f"Native compaction did not produce a summary: {event}"
                )
            return
        rpc.read_available(0.5)
    raise TimeoutError("Native SDK /compact did not finish")


def run(args, case):
    out = args.out / case
    out.mkdir(parents=True, exist_ok=False)
    root, task_dir = out / "records", out / "task"
    store = VistaStore(root)
    fixed = case.startswith("fixed-")
    task = None if fixed else TaskHistory(store, task_dir, task_id=TASK_ID)
    current, target, similar = (
        out / f"{name}.png" for name in ("current", "target", "similar")
    )
    picture(current, 62, (139, 82))
    picture(target, 39, (173, 179))
    picture(similar, 43, (82, 173))
    if fixed:
        first_record = store.save_record(png=current.read_bytes(), label="当前对比图")
    else:
        task.save("seed-current", png=current.read_bytes(), label="当前对比图")
    save = case in {"controlled", "notes", "native", "offload"}
    metadata = {"target_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    if not save:
        original = store.save_record(png=target.read_bytes(), label=LABEL)
        if fixed:
            other = store.save_record(png=similar.read_bytes(), label="相近轮廓备选")
            records = [
                {
                    "short_id": "R1",
                    "record": first_record["record"],
                    "receipt": "已存 R1 · 当前对比图",
                    "label": "当前对比图",
                },
                {
                    "short_id": "R2",
                    "record": original["record"],
                    "receipt": RECEIPT,
                    "label": LABEL,
                },
                {
                    "short_id": "R3",
                    "record": other["record"],
                    "receipt": "已存 R3 · 相近轮廓备选",
                    "label": "相近轮廓备选",
                },
            ]
        else:
            task.import_record("seed-target", original["record"])
    marker = out / "phase.txt"
    marker.write_text("history")
    sid = str(uuid4())
    cfg = {
        "sessionId": sid,
        "taskId": TASK_ID,
        "taskDir": str(task_dir),
        "vistaRoot": str(root),
        "python": str(args.python),
        "tokenizerPath": str(args.tokenizer),
        "auditPath": str(out / "wire.jsonl"),
        "phasePath": str(marker),
        "selected": "R1",
        "mode": "query" if case == "fixed-query" else "auto",
        "captureText": True,
        "readOnly": not save,
        "maxModelRequests": 12 if case == "notes" else 9,
    }
    if fixed:
        cfg["fixedView"] = {
            "records": records,
            "view": {
                "view_id": "fixed-line",
                "text": "R2 · 用户认可过这版留白",
                "covered_short_ids": ["R2"],
            },
        }
    history_text = "这是同一图形任务的一份旧草图。请保留简短任务背景即可。" + (
        "　".join(f"成员记录 {i}：核对内部交付所需项目。" for i in range(90))
        if case == "native"
        else ""
    )
    seed = {
        "sessionId": sid,
        "runtimePackage": RUNTIME,
        "phasePath": str(marker),
        "targetPath": str(target),
        "label": LABEL,
        "receipt": RECEIPT,
        "historyText": history_text,
        "save": save,
    }
    patches = overlay(cfg, seed)
    patches[-1]["insert"][-2]["name"] = str(PROBE)
    if case == "native":
        patches[-1]["insert"][:0] = [
            {"id": "commands", "name": "@deepseek-ai/dsh-commands"},
            {"id": "command-compact", "name": "@deepseek-ai/dsh-command-compact"},
        ]
        patches.insert(0, {"id": "compaction-basic", "config": {"maxTokens": 4096}})
    if case == "offload":
        provider = next(
            row for row in patches[-1]["insert"] if row["id"] == "account-models"
        )
        provider["config"].update(maxImagesPerRequest=1, imageOffloadCountQuantum=1)
    save_report(out / "overlay.json", patches)
    with tempfile.TemporaryDirectory(prefix="vista-history-probe-") as temp:
        workspace = Path(temp) / "work"
        workspace.mkdir()
        rpc = RpcProcess(
            [
                args.cli,
                "--profile",
                "sdk-minimal",
                "--patch",
                str(out / "overlay.json"),
            ],
            Path(temp) / "home",
            workspace,
        )
        try:
            rpc.request(
                "initialize", {"cwd": str(workspace), **MODEL}, time.monotonic() + 60
            )
            phase_prompt(rpc, sid, marker, "history", SIMPLE)
            if case == "fixed-sequential":
                phase_prompt(rpc, sid, marker, "image-only", SIMPLE)
                phase_prompt(rpc, sid, marker, "last-clue", SIMPLE)
                phase_prompt(rpc, sid, marker, "stable", SIMPLE)
            elif fixed:
                phase_prompt(rpc, sid, marker, "feedback", IMPLICIT, current)
            elif case == "controlled":
                task.save(
                    "seed-similar",
                    png=similar.read_bytes(),
                    label="边缘轮廓样稿",
                    subtask_id="archive-set",
                    subtask_label="早期留白",
                )
                phase_prompt(rpc, sid, marker, "image-only", SIMPLE)
                phase_prompt(rpc, sid, marker, "last-clue", SIMPLE)
                phase_prompt(rpc, sid, marker, "stable", SIMPLE)
                phase_prompt(rpc, sid, marker, "directory-lost", SIMPLE)
            elif case == "simultaneous":
                phase_prompt(rpc, sid, marker, "simultaneous", SIMPLE)
            elif case == "native":
                compact(rpc, sid, marker)
                phase_prompt(rpc, sid, marker, "after-native-compact", SIMPLE, current)
            elif case == "offload":
                phase_prompt(rpc, sid, marker, "native-offload", SIMPLE, current)
            elif case == "notes":
                phase_prompt(
                    rpc, sid, marker, "note-explicit", "R2 的宽度刚好。只回复收到。"
                )
                phase_prompt(rpc, sid, marker, "note-reply", "留白很好。只回复收到。")
                phase_prompt(
                    rpc, sid, marker, "note-ambiguous", "刚才那张不错。只回复收到。"
                )
                phase_prompt(
                    rpc,
                    sid,
                    marker,
                    "agent-observation",
                    "请用观察工具记录你实际看见的 R2 图形，只写你自己的观察。",
                    target,
                )
            if case == "notes":
                catalog = HistoryCatalog(task)
                view = catalog.short_view()
                notes = catalog.expand(view_id=view.view_id, target="R2").notes
                metadata["notes"] = [note.__dict__ for note in notes]
            rpc.request("shutdown", {}, time.monotonic() + 30)
            rpc.process.wait(timeout=10)
            metadata["complete"] = True
        finally:
            rpc.close()
            save_report(
                out / "events.json",
                {
                    **metadata,
                    "notifications": rpc.notifications,
                    "stderr": rpc.stderr,
                },
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cli", default=DEFAULT_CLI)
    parser.add_argument(
        "cases",
        nargs="+",
        choices=(
            "fixed-query",
            "fixed-auto",
            "fixed-sequential",
            "controlled",
            "simultaneous",
            "native",
            "offload",
            "notes",
        ),
    )
    args = parser.parse_args()
    failed = False
    for case in args.cases:
        try:
            run(args, case)
            print(f"{case}: completed", flush=True)
        except Exception as error:
            failed = True
            print(f"{case}: {error}", flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
