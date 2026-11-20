"""Run the frozen image-history comparisons through native DSH JSON-RPC."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from dsh_sdk_run import DEFAULT_CLI, RpcProcess, prompt_images, save_report, timestamp
from vista_history_fixtures import freeze_task

REPO = Path(__file__).resolve().parents[1]
MODEL = {
    "provider": "deepseek-account",
    "model": "deepseek-flash",
    "reasoningEffort": "high",
    "maxTokens": 4096,
}
BUDGET = {"budget": 400, "imageLimit": 3, "maxModelRequests": 7}
RUNTIME = (
    "/Applications/DeepSeek Harness.app/Contents/Resources/app.asar/dsh/package.json"
)
ARMS = {
    "A": dict(grouped=False, includeNotes=False, mode="auto"),
    "B1": dict(grouped=True, includeNotes=False, mode="auto"),
    "B2": dict(grouped=True, includeNotes=True, mode="auto"),
    "query": dict(grouped=True, includeNotes=True, mode="query"),
    "auto": dict(grouped=True, includeNotes=True, mode="auto"),
}


def freeze(root: Path, tokenizer: Path) -> dict:
    from tokenizers import Tokenizer

    from vista_memory import VistaStore
    from vista_memory.history_catalog import HistoryCatalog
    from vista_memory.history_store import TaskHistory

    codec = Tokenizer.from_file(str(tokenizer))
    sets = []
    for index in range(2):
        task = root / f"set-{index + 1}"
        materials = freeze_task(task, index)
        history = TaskHistory(
            VistaStore(task / "records"), task / "task", task_id=materials["task"]
        )
        catalog = HistoryCatalog(
            history,
            count_tokens=lambda text: len(
                codec.encode(text, add_special_tokens=False).ids
            ),
        )
        views = {}
        for arm in ("A", "B1", "B2"):
            view = catalog.short_view(
                selected=materials["current"],
                subtask_id=materials["current_topic"],
                grouped=ARMS[arm]["grouped"],
                include_notes=ARMS[arm]["includeNotes"],
                budget=BUDGET["budget"],
            )
            if set(view.covered_short_ids) != set(materials["records"]):
                raise RuntimeError(f"{arm}: Incomplete fixture view")
            if arm != "A" and (
                "R4" in view.visible_short_ids
                or not any(
                    key != "rest" and "R4" in values
                    for key, values in view.entries.items()
                )
            ):
                raise RuntimeError(f"{arm}: The folded target must be behind its topic")
            views[arm] = asdict(view)
        materials["views"] = views
        sets.append(materials)

    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "created": timestamp(),
        "model": MODEL,
        "limits": BUDGET,
        "tokenizer_sha256": hashlib.sha256(tokenizer.read_bytes()).hexdigest(),
        "checkpoint_source": "controlled DSH surface replacement",
        "sets": sets,
    }
    save_report(root / "frozen.json", manifest)
    return manifest


def conditions(manifest: dict):
    for index, materials in enumerate(manifest["sets"]):
        for case in materials["cases"]:
            if case["id"] == "delivery":
                arms = ("query", "auto") if index == 0 else ("auto", "query")
            else:
                arms = ("A", "B1", "B2") if index == 0 else ("B2", "B1", "A")
            for arm in arms:
                identifier = f"{materials['task']}-{case['id']}-{arm}"
                yield identifier, materials, case, arm


def overlay(config: dict, seed: dict, credentials_file: Path | None = None) -> list:
    baseline = REPO / "docs/test-results/20261116-dsh/sdk-overlay.json"
    entries = json.loads(baseline.read_text())[:-1]
    if credentials_file is None:
        dsh_home = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh")
        credentials_file = dsh_home / ".credentials.yaml"
    entries += [
        {"id": "session-log-deepseek", "disabled": True},
        {"id": "plugin-package-inventory-deepseek", "disabled": True},
        {
            "insert": [
                {
                    "id": "credentials-local",
                    "name": "@deepseek-ai/dsh-credentials-local",
                    "config": {
                        "path": str(credentials_file.expanduser().resolve()),
                        "watch": False,
                    },
                },
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
                    "id": "vista-history-checkpoint",
                    "name": str(
                        Path(__file__).with_name("vista_history_checkpoint.mjs")
                    ),
                    "config": seed,
                },
                {
                    "id": "vista-image-history",
                    "name": str(Path(__file__).with_name("vista_history_dsh.mjs")),
                    "config": config,
                },
            ]
        },
    ]
    return entries


def completed(report: dict) -> bool:
    if "error" in report or not report.get("finished"):
        return False
    end = next(
        (event for event in reversed(report["events"]) if event["type"] == "turn/end"),
        None,
    )
    return end is not None and end["data"]["reason"]["kind"] == "completed"


def run_case(args, condition: tuple) -> bool:
    identifier, materials, case, arm = condition
    evidence = args.root / "runs" / identifier
    evidence.mkdir(parents=True, exist_ok=False)
    task = args.root / materials["task"]
    session = str(uuid4())
    archived = case.get("target", "R3")
    frozen_view = materials["views"]["B2" if arm in {"auto", "query"} else arm]
    asset = f"{materials['current']}.png"
    report = {
        "id": identifier,
        "arm": arm,
        "case": case["id"],
        "session": session,
        "started": timestamp(),
    }
    rpc = None
    try:
        with tempfile.TemporaryDirectory(prefix="vista-history-dsh-") as temp:
            home, workspace = Path(temp) / "home", Path(temp) / "work"
            home.mkdir()
            workspace.mkdir()
            config = {
                "sessionId": session,
                "taskId": materials["task"],
                "taskDir": str(task / "task"),
                "vistaRoot": str(task / "records"),
                "python": str(args.python),
                "tokenizerPath": str(args.tokenizer),
                "auditPath": str(evidence / "wire.jsonl"),
                "captureText": True,
                "selected": materials["current"],
                "subtaskId": materials["current_topic"],
                "readOnly": True,
                "frozenViewId": frozen_view["view_id"],
                **ARMS[arm],
                **BUDGET,
            }
            archived_label = materials["records"][archived]["label"]
            seed = {
                "runtimePackage": RUNTIME,
                "sessionId": session,
                "image": str(task / "assets" / f"{archived}.png"),
                "receipt": f"已存 {archived} · {archived_label}",
                "summary": materials["checkpoint"],
            }
            save_report(
                evidence / "overlay.json", overlay(config, seed, args.credentials_file)
            )
            report["workspace"] = str(workspace)
            command = [
                args.cli,
                "--profile",
                "sdk-minimal",
                "--patch",
                str(evidence / "overlay.json"),
            ]
            rpc = RpcProcess(command, home, workspace)
            try:
                rpc.request(
                    "initialize",
                    {"cwd": str(workspace), **MODEL},
                    time.monotonic() + 60,
                )
                images, _ = prompt_images(
                    [
                        {
                            "path": str(task / "assets" / asset),
                            "name": "current.png",
                        }
                    ]
                )
                blocks = [{"type": "text", "text": case["question"]}, *images]
                deadline = time.monotonic() + 260
                rpc.request(
                    "session/prompt",
                    {"sessionId": session, "contentBlocks": blocks},
                    deadline,
                )
                rpc.request("session/wait", {"sessionId": session}, deadline)
                rpc.request("shutdown", {}, time.monotonic() + 12)
            finally:
                rpc.close()
    except (OSError, RuntimeError, TimeoutError) as exc:
        report["error"] = str(exc)
    finally:
        report["events"] = [
            row["params"]["event"]
            for row in (rpc.notifications if rpc else [])
            if row.get("method") == "session.event"
            and row["params"]["sessionId"] == session
        ]
        if rpc:
            report["stderr"] = rpc.stderr
        report["finished"] = timestamp()
        save_report(evidence / "run.json", report)
    success = completed(report)
    print(f"{identifier} {'completed' if success else 'failed'}", flush=True)
    return success


def run_or_reuse(args, condition: tuple) -> bool:
    identifier = condition[0]
    evidence = args.root / "runs" / identifier
    locks = args.root / "run-locks"
    locks.mkdir(exist_ok=True)
    with (locks / identifier).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"{identifier} is already running", file=sys.stderr)
            return False
        if evidence.exists():
            try:
                previous = json.loads((evidence / "run.json").read_text())
                if previous.get("id") == identifier and completed(previous):
                    return True
            except (AttributeError, KeyError, OSError, TypeError, ValueError):
                pass
            if not args.retry_failed:
                print(
                    f"{identifier}: no successful report. Use --retry-failed to rerun.",
                    file=sys.stderr,
                )
                return False
            archived = args.root / "retry-evidence" / f"{identifier}-{uuid4().hex}"
            archived.parent.mkdir(exist_ok=True)
            evidence.rename(archived)
            print(f"Archived previous attempt at {archived}", flush=True)
        return run_case(args, condition)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=lambda value: Path(value).resolve())
    parser.add_argument(
        "--tokenizer", required=True, type=lambda value: Path(value).resolve()
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--credentials-file",
        type=Path,
        help="Credentials file (default: .credentials.yaml in DSH_HOME or ~/.dsh)",
    )
    parser.add_argument("--cli", default=DEFAULT_CLI)
    parser.add_argument("--jobs", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--only", nargs="+")
    parser.add_argument(
        "--retry-failed", action="store_true", help="Archive and rerun failed cases"
    )
    args = parser.parse_args()
    path = args.root / "frozen.json"
    manifest = (
        json.loads(path.read_text())
        if path.exists()
        else freeze(args.root, args.tokenizer)
    )
    if (
        hashlib.sha256(args.tokenizer.read_bytes()).hexdigest()
        != manifest["tokenizer_sha256"]
    ):
        parser.error("Tokenizer differs from the frozen evidence")
    if args.prepare:
        return 0
    selected = [
        row for row in conditions(manifest) if args.only is None or row[0] in args.only
    ]
    if args.only and {row[0] for row in selected} != set(args.only):
        parser.error("Unknown experiment case")
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda row: run_or_reuse(args, row), selected))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
