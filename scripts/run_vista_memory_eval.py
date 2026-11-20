"""Run paired VISTA memory experiments through isolated DSH SDK processes."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import random
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dsh_sdk_run import save_report
from verify_vista_freeze import verify_freeze
from vista_eval_paths import load_dataset

REPO = Path(__file__).resolve().parents[1]


def now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def run_one(case: dict, condition: str, repeat: int, args, lock_fd: int) -> dict:
    if args.freeze:
        verify_freeze(args.freeze)
    run_id = f"{case['id']}-r{repeat}-{condition}-a{args.attempt}"
    folder = args.output / args.phase / "runs" / run_id
    result_path = folder / "run.json"
    if result_path.exists():
        existing = json.loads(result_path.read_text())
        if existing["status"] not in {"completed", "failed"}:
            raise RuntimeError(f"Attempt has no terminal report: {run_id}")
        return {"run_id": run_id, "status": existing["status"], "reused": True}
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pair-lock-used").touch()
    runtime = Path(tempfile.mkdtemp(prefix=f"vista-eval-{run_id}-", dir="/private/tmp"))
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    base = json.loads(args.overlay.read_text())
    save_report(folder / "expected-codes.json", [])
    for row in base:
        for entry in row.get("insert", []):
            if entry["id"] == "mcp-vista":
                entry["config"].update(
                    command=sys.executable,
                    cwd=str(REPO),
                    args=[
                        str(REPO / "scripts/vista_eval_mcp.py"),
                        "--root",
                        case["root"],
                        "--condition",
                        condition,
                        "--audit-log",
                        str(folder / "mcp-audit.jsonl"),
                    ],
                )
            elif entry["id"] == "vista-request-audit":
                entry["name"] = str(REPO / "scripts/dsh_request_audit.mjs")
                entry["config"].update(
                    expectedCodesPath=str(folder / "expected-codes.json"),
                    outputPath=str(folder / "http-audit.jsonl"),
                )
    save_report(folder / "overlay.json", base)
    request = {
        "id": run_id,
        "session_id": run_id,
        "text": case["prompt"],
        "timeout_seconds": 300,
        "images": [
            {"path": case["target_path"], "name": "target.png"},
            {"path": case["current_path"], "name": "current.png"},
        ],
    }
    save_report(folder / "case-input.json", [request])
    metadata = {
        "run_id": run_id,
        "case_id": case["id"],
        "task_type": case["task_type"],
        "scenario": case["scenario"],
        "scene_id": case["scene_id"],
        "condition": condition,
        "repeat": repeat,
        "attempt": args.attempt,
        "runtime": str(runtime),
        "workspace": str(workspace),
        "started_at": now(),
        "prompt_sha256": hashlib.sha256(case["prompt"].encode()).hexdigest(),
    }
    save_report(folder / "metadata.json", metadata)
    command = [
        sys.executable,
        str(REPO / "scripts/dsh_sdk_run.py"),
        "--home",
        str(runtime / "home"),
        "--overlay",
        str(folder / "overlay.json"),
        "--workspace",
        str(workspace),
        "--cases",
        str(folder / "case-input.json"),
        "--output",
        str(result_path),
        "--model",
        "gpt-6.1-sol",
        "--provider",
        "openai",
        "--reasoning",
        "high",
        "--max-tokens",
        "8192",
    ]
    with (folder / "driver.log").open("w") as output:
        process = subprocess.run(
            command, cwd=REPO, stdout=output, stderr=output, pass_fds=(lock_fd,)
        )
    result = json.loads(result_path.read_text())
    metadata.update(
        finished_at=now(), exit_code=process.returncode, status=result["status"]
    )
    save_report(folder / "metadata.json", metadata)
    print(f"{run_id} {result['status']}", flush=True)
    return metadata


def run_pair(pair: dict, args) -> list[dict]:
    """Keep both conditions at the same attempt, including after a crash."""
    stem = f"{pair['case']['id']}-r{pair['repeat']}"
    runs = args.output / args.phase / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    with (runs / f".{stem}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return [{"pair": stem, "status": "running"}]
        attempt = args.attempt
        while True:
            interrupted = False
            for condition in pair["conditions"]:
                folder = runs / f"{stem}-{condition}-a{attempt}"
                if not folder.exists():
                    continue
                path = folder / "run.json"
                status = (
                    json.loads(path.read_text())["status"] if path.exists() else None
                )
                if status in {"completed", "failed"}:
                    continue
                # Older drivers did not inherit the pair lock. Check the
                # exact attempt path before retrying a legacy running report.
                if not (folder / "pair-lock-used").exists():
                    commands = subprocess.check_output(
                        ["ps", "-axo", "command="], text=True
                    )
                    if any(
                        str(path) in cmd
                        and "dsh_sdk_run.py" in cmd
                        or str(folder / "overlay.json") in cmd
                        and "--patch" in cmd
                        for cmd in commands.splitlines()
                    ):
                        return [{"run_id": folder.name, "status": "running"}]
                interrupted = True
            if not interrupted:
                break
            attempt += 1
        actual_args = argparse.Namespace(**{**vars(args), "attempt": attempt})
        return [
            run_one(pair["case"], cond, pair["repeat"], actual_args, lock.fileno())
            for cond in pair["conditions"]
        ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=["calibration", "formal"], required=True)
    parser.add_argument(
        "--overlay",
        type=Path,
        default=REPO / "docs/test-results/20261116-dsh/sdk-overlay.json",
    )
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("/private/tmp/vista-eval-common-workspace-20261116"),
    )
    parser.add_argument("--seed", type=int, default=20261116)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument(
        "--repeats",
        type=int,
        nargs="+",
        choices=[1, 2],
        help="Optional repeat numbers to run",
    )
    parser.add_argument(
        "--case-ids", help="Optional comma-separated subset for paired retries"
    )
    parser.add_argument("--freeze", type=Path)
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.phase == "formal" and args.freeze is None:
        parser.error("Formal runs require the frozen experiment manifest")
    if args.jobs < 1:
        parser.error("jobs must be positive")
    dataset = load_dataset(args.dataset)
    cases = [case for case in dataset["cases"] if case["split"] == args.phase]
    if args.case_ids:
        requested = set(args.case_ids.split(","))
        cases = [case for case in cases if case["id"] in requested]
        if {case["id"] for case in cases} != requested:
            parser.error("Requested cases do not match this dataset split")
    if not cases:
        parser.error("No cases selected")
    repeats = [1] if args.phase == "calibration" else [1, 2]
    if args.repeats:
        selected_repeats = args.repeats
        if (
            not selected_repeats
            or len(set(selected_repeats)) != len(selected_repeats)
            or not set(selected_repeats) <= set(repeats)
        ):
            parser.error("Selected repeats must be unique repeats from this phase")
        repeats = selected_repeats
    pairs = [(case, repeat) for case in cases for repeat in repeats]
    rng = random.Random(args.seed)
    rng.shuffle(pairs)
    schedule = []
    for case, repeat in pairs:
        conditions = ["text", "visual"]
        rng.shuffle(conditions)
        schedule.append({"case": case, "repeat": repeat, "conditions": conditions})
    if args.freeze:
        verify_freeze(args.freeze)
    save_report(args.output / args.phase / f"schedule-a{args.attempt}.json", schedule)

    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run_pair, pair, args) for pair in schedule]
        for future in as_completed(futures):
            results.extend(future.result())
            save_report(
                args.output / args.phase / f"execution-a{args.attempt}.json", results
            )
    return 0 if all(result["status"] == "completed" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
