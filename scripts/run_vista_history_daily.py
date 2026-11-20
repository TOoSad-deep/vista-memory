"""Exercise VISTA through ordinary user turns in the native DSH SDK."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from uuid import uuid4

from dsh_sdk_run import DEFAULT_CLI, RpcProcess, prompt_images, save_report
from run_vista_history_experiment import MODEL, overlay
from vista_history_daily_fixtures import freeze_daily_fixtures
from vista_history_dsh_probe import compact

from vista_memory import VistaStore
from vista_memory.history_catalog import HistoryCatalog
from vista_memory.history_store import TaskHistory

ENTRY = Path(__file__).with_name("vista_history_daily_entry.mjs")
IMAGE_LIMIT = 3
BASE_REQUEST_CAP = 24
BRANCH_REQUEST_CAP = 9
MEMOS = (
    "先把这个项目的交接说明整理成三段。设计负责发布图，运营负责给出最终文案，"
    "技术要确认导出的资源能在手机和桌面端同时显示。"
    "请给每段写一句交付责任，并列一个该角色最容易漏掉的确认项。",
    "这次审核只看排版、日期和链接可达性。审核通过后需要保留可编辑源稿，"
    "并给发布者一个已编号的导出文件。请把审核顺序和各步骤负责人的确认点写清楚。"
    "安排一个发布前可以撤回修改的时间点。",
    "给同事写一份发布前检查清单。小屏优先看按钮可点范围和正文换行，"
    "大屏优先看标题、页边距和图文对齐。链接请区分原页面和预览地址，"
    "同时提醒校对活动时间、时区、联系方式和文案里的专名。",
    "源文件、发布文件和归档记录的命名要能区分线下看样和正式发布。"
    "用月份、项目短名、用途和版本号做一个可搜索的命名例子，再说明改稿时谁该更新交接记录。"
    "文档不要要求他们安装额外软件。",
    "现在把交付的说明改成面向第一次接手的同事。需要包含工作目录、文件责任人、"
    "链接确认方式和错误恢复步骤。项目留一个工作日上午给校对。"
    "整理成五条，每条要具体，避免只写‘检查质量’。",
    "有两种发布故障需要事先想好。第一种是预览正常但上线链接变更。"
    "第二种是负责人改完文案但缓存里仍显示上一版。为每种情况写客服回应、技术排查顺序和恢复后的回执。",
    "请为这个项目写一份简洁的最终交付邮件，收件人是运营和设计同事。"
    "先交代这是可供审核的交付包，然后列确认项和回复截止时间。"
    "把需要验收的三件事分别写成可判断的句子。",
    "交付之后还要做一次隔日复核。写一个从发布记录到回看导出文件的简短流程，"
    "如果负责人不在，交接给值班同事处理。最后加一句提醒，记录最终确认时间。",
)


def write_overlay(args, out, session, store, task_dir, task_id, cap):
    config = {
        "sessionId": session,
        "taskId": task_id,
        "taskDir": str(task_dir),
        "vistaRoot": str(store),
        "python": str(args.python),
        "tokenizerPath": str(args.tokenizer),
        "auditPath": str(out / "wire.jsonl"),
        "phasePath": str(out / "phase.txt"),
        "captureText": True,
        "mode": "auto",
        "imageLimit": IMAGE_LIMIT,
        "maxModelRequests": cap,
    }
    patches = overlay(config, {}, args.credentials_file)
    entries = patches[-1]["insert"]
    entries[-2] = {
        "id": "vista-history-daily-entry",
        "name": str(ENTRY),
        "config": {"sessionId": session, "auditPath": str(out / "entry.jsonl")},
    }
    entries[:0] = [
        {"id": "commands", "name": "@deepseek-ai/dsh-commands"},
        {"id": "command-compact", "name": "@deepseek-ai/dsh-command-compact"},
    ]
    path = out / "overlay.json"
    save_report(path, patches)
    return path


def open_rpc(args, out, session, store, task_dir, task_id, cap, workspace):
    path = write_overlay(args, out, session, store, task_dir, task_id, cap)
    return RpcProcess(
        [args.cli, "--profile", "sdk-minimal", "--patch", str(path)],
        out / "home",
        workspace,
    )


def events_since(rpc, offset, session):
    return [
        row["params"]["event"]
        for row in rpc.notifications[offset:]
        if row["method"] == "session.event" and row["params"]["sessionId"] == session
    ]


def prompt(rpc, out, session, phase, text, image=None):
    (out / "phase.txt").write_text(phase)
    blocks = [{"type": "text", "text": text}]
    if image:
        blocks += prompt_images([{"path": str(image), "name": "draft.png"}])[0]
    start = len(rpc.notifications)
    deadline = time.monotonic() + 155
    rpc.request(
        "session/prompt", {"sessionId": session, "contentBlocks": blocks}, deadline
    )
    rpc.request("session/wait", {"sessionId": session}, deadline)
    events = events_since(rpc, start, session)
    reason = next(
        (e["data"]["reason"] for e in reversed(events) if e["type"] == "turn/end"),
        {"kind": "missing-end"},
    )
    print(f"{out.parent.name}/{phase}: {reason['kind']}", flush=True)
    return {"phase": phase, "reason": reason, "events": events}


def require_success(result):
    if result["reason"]["kind"] != "completed":
        raise RuntimeError(f"{result['phase']}: {result['reason']}")


def read_wire(out):
    path = out / "wire.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def close_rpc(rpc, normal):
    try:
        if normal:
            rpc.request("shutdown", {}, time.monotonic() + 12)
            rpc.process.wait(timeout=10)
    finally:
        rpc.close()


def age_and_compact(rpc, out, session, report):
    done = {turn["phase"] for turn in report["turns"]}
    for index, memo in enumerate(MEMOS, 1):
        phase = f"daily-{index}"
        if phase in done:
            continue
        result = prompt(rpc, out, session, phase, memo)
        report["turns"].append(result)
        # An output-length stop leaves a usable conversation for the next turn.
        if result["reason"]["kind"] != "max-tokens":
            require_success(result)
    report["before_manual"] = [
        event
        for turn in report["turns"]
        for event in turn["events"]
        if event["type"].startswith("compaction/")
    ]
    report["compaction"] = list(report["before_manual"])
    if any(e["type"] == "compaction/summary" for e in report["before_manual"]):
        report["checkpoint_source"] = "automatic"
    else:
        report["checkpoint_source"] = "manual-native-compact"
        offset = len(rpc.notifications)
        compact(rpc, session, out / "phase.txt")
        report["compaction"].extend(
            e
            for e in events_since(rpc, offset, session)
            if e["type"].startswith("compaction/")
        )


def seed(args, material, out, root, workspace):
    out.mkdir()
    session = str(uuid4())
    task_id = material["id"]
    store = root / "records"
    task_dir = out / "task"
    history = TaskHistory(VistaStore(store), task_dir, task_id=task_id)
    report = {"session": session, "writes": {}, "turns": [], "before_manual": []}
    rpc = open_rpc(
        args, out, session, store, task_dir, task_id, BASE_REQUEST_CAP, workspace
    )
    ready = False
    try:
        rpc.request(
            "initialize", {"cwd": str(workspace), **MODEL}, time.monotonic() + 60
        )
        drafts = material["drafts"]
        for draft in drafts:
            count = len(history.list_records())
            name = draft["id"]
            save_request = (
                f"保存图片：{draft['label']}\n只回复收到，图案细节留到后面再看。"
            )
            if name == material["current"]:
                save_request += "\n接下来用这版作为当前修改稿。"
            result = prompt(
                rpc, out, session, f"save-{name}", save_request, draft["path"]
            )
            report["turns"].append(result)
            require_success(result)
            records = history.list_records()
            if len(records) != count + 1:
                raise RuntimeError(
                    f"Native save did not register one record for {name}"
                )
            item = records[-1]
            image = history.read(item.short_id).image
            digest = hashlib.sha256(image).hexdigest()
            if digest != draft["sha256"] or item.label != draft["label"]:
                raise RuntimeError(f"Native save bound different content for {name}")
            report["writes"][name] = {
                "short_id": item.short_id,
                "sha256": digest,
                "receipt": item.receipt,
            }
            if name == material["accepted"]:
                feedback = material["feedback_template"].format(short_id=item.short_id)
                # Entry maps the explicit short ID in the end user's ordinary prompt.
                note = feedback.removeprefix(f"{item.short_id} ")
                result = prompt(
                    rpc, out, session, "feedback", f"评价 {item.short_id}：{note}"
                )
                report["turns"].append(result)
                require_success(result)
                catalog = HistoryCatalog(history)
                view = catalog.short_view()
                notes = catalog.expand(view_id=view.view_id, target=item.short_id).notes
                user_notes = [n for n in notes if n.source == "user"]
                if len(user_notes) != 1 or item.short_id not in user_notes[0].text:
                    raise RuntimeError(
                        "Native feedback did not bind to the accepted draft"
                    )
                report["feedback"] = {
                    "short_id": item.short_id,
                    "text": user_notes[0].text,
                }
        age_and_compact(rpc, out, session, report)
        ready = True
    finally:
        try:
            close_rpc(rpc, ready)
        finally:
            report["stderr"] = rpc.stderr
            save_report(out / "seed.json", report)
    return report


def branch(args, material, case, root, workspace, parent):
    seed_root = root / "seed"
    out = root / case["id"]
    out.mkdir()
    shutil.copytree(seed_root / "home", out / "home")
    shutil.copytree(seed_root / "task", out / "task")
    shutil.copy2(seed_root / "wire.jsonl", out / "wire.jsonl")
    starting_requests = max((row.get("n", 0) for row in read_wire(out)), default=0)
    session = parent["session"]
    report = {
        "id": case["id"],
        "session": session,
        "starting_requests": starting_requests,
    }
    rpc = open_rpc(
        args,
        out,
        session,
        root / "records",
        out / "task",
        material["id"],
        starting_requests + BRANCH_REQUEST_CAP,
        workspace,
    )
    ready = False
    try:
        rpc.request(
            "initialize", {"cwd": str(workspace), **MODEL}, time.monotonic() + 60
        )
        rpc.request("session/resume", {"sessionId": session}, time.monotonic() + 60)
        report["turn"] = prompt(rpc, out, session, case["id"], case["question"])
        if case["id"] == "visual" and report["turn"]["reason"]["kind"] == "completed":
            report["followup"] = prompt(
                rpc, out, session, "followup", "先暂停图稿核对。请只回复‘收到，暂停’。"
            )
        ready = True
    except (OSError, RuntimeError, TimeoutError) as error:
        report["error"] = str(error)
        report["partial_events"] = events_since(rpc, 0, session)
    finally:
        try:
            close_rpc(rpc, ready)
        finally:
            report["stderr"] = rpc.stderr
            save_report(out / "case.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=lambda value: Path(value).resolve())
    parser.add_argument(
        "--tokenizer", type=lambda value: Path(value).resolve(), required=True
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--cli", default=DEFAULT_CLI)
    parser.add_argument("--only", choices=("set-1", "set-2"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    material_sets = freeze_daily_fixtures(args.out / "materials")
    save_report(
        args.out / "frozen.json",
        {"sets": material_sets, "model": MODEL, "image_limit": IMAGE_LIMIT},
    )
    failed = False
    for material in material_sets:
        if args.only is not None and material["id"] != args.only:
            continue
        root = args.out / material["id"]
        workspace = root / "workspace"
        workspace.mkdir(parents=True)
        try:
            parent = seed(args, material, root / "seed", root, workspace)
        except (OSError, RuntimeError, TimeoutError) as error:
            save_report(root / "error.json", {"stage": "seed", "error": str(error)})
            print(f"{material['id']} seed: {error}", file=sys.stderr, flush=True)
            failed = True
            continue
        for case in material["cases"]:
            report = branch(args, material, case, root, workspace, parent)
            failed |= (
                report.get("turn", {}).get("reason", {}).get("kind") != "completed"
            )
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
