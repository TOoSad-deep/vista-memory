"""Run the predeclared Shader recall conditions sequentially with frozen inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
from pathlib import Path

from dsh_sdk_run import save_report, timestamp

PROJECT = Path(__file__).resolve().parents[1]


def source_manifest() -> dict[str, str]:
    paths = list((PROJECT / "src/vista_memory").glob("*.py"))
    paths += list((PROJECT / "scripts").glob("shader_recall_*.*"))
    paths += [
        Path(__file__),
        PROJECT / "scripts/run_shader_recall.py",
        PROJECT / "scripts/dsh_sdk_run.py",
        PROJECT / "scripts/dsh_request_audit.mjs",
    ]
    paths += list((PROJECT / "scripts/shader_recall_python").glob("*.py"))
    business = PROJECT.parent / "dsh-shader-11-13"
    for directory, extension in [
        ("src", "*.js"),
        ("runtime", "*.py"),
        ("skills", "*.md"),
    ]:
        paths += list((business / directory).rglob(extension))
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(set(paths))
        if path.is_file()
    }


def make_schedule(cases: list[dict], phase: str, seed: int) -> list[dict]:
    repeats = [1] if phase == "calibration" else [1, 2]
    blocks = []
    for case in cases:
        for repeat in repeats:
            variants = []
            if phase == "end-to-end":
                variants = [
                    {
                        "condition": "natural",
                        "access_view": "known",
                        "image_condition": image,
                    }
                    for image in ["visual", "text"]
                ]
            else:
                views = (
                    ["known", "discovery"]
                    if case["purpose"] == "opportunity"
                    else ["known"]
                )
                for view in views:
                    for condition in ["natural", "reminded"]:
                        variants.append(
                            {
                                "condition": condition,
                                "access_view": view,
                                "image_condition": "visual",
                            }
                        )
                variants.append(
                    {
                        "condition": "provided",
                        "access_view": "known",
                        "image_condition": "visual",
                    }
                )
            blocks.append({"case": case, "repeat": repeat, "variants": variants})
    rng = random.Random(seed)
    rng.shuffle(blocks)
    for block in blocks:
        rng.shuffle(block["variants"])
    return [
        {"case": block["case"], "repeat": block["repeat"], **variant}
        for block in blocks
        for variant in block["variants"]
    ]


def execute(args: argparse.Namespace) -> int:
    args.output.mkdir(parents=True, exist_ok=False)
    dataset = json.loads(args.manifest.read_text())
    cases = dataset["cases"]
    if args.phase == "end-to-end":
        cases = [case for case in cases if case.get("split") == "end-to-end"]
    elif any("purpose" not in case or "provided_records" not in case for case in cases):
        raise ValueError("Checkpoint cases require purpose and provided_records")
    if not cases:
        raise ValueError("No cases selected")
    schedule = make_schedule(cases, args.phase, args.seed)
    if args.limit:
        schedule = schedule[: args.limit]
    frozen = source_manifest()
    save_report(
        args.output / "freeze.json",
        {"created_at": timestamp(), "sources": frozen, "dataset": dataset},
    )
    save_report(args.output / "schedule.json", schedule)
    report = {
        "started_at": timestamp(),
        "phase": args.phase,
        "status": "running",
        "expected_runs": len(schedule),
        "runs": [],
    }
    save_report(args.output / "execution.json", report)
    for index, item in enumerate(schedule, 1):
        if source_manifest() != frozen:
            report["status"] = "source-changed"
            break
        run_directory = args.output / "runs" / f"{index:03}"
        command = [
            sys.executable,
            str(PROJECT / "scripts/run_shader_recall.py"),
            "probe" if args.phase == "end-to-end" else "checkpoint",
            "--output",
            str(run_directory),
            "--provider",
            "deepseek-account",
            "--model",
            "deepseek-flash",
        ]
        if args.phase == "end-to-end":
            command += [
                "--model-turn",
                "--image-control",
                "--generation-task",
                "--reference",
                item["case"]["target_original"],
                "--max-render-attempts",
                "8",
                "--max-model-requests",
                "24",
                "--timeout",
                "1800",
                "--max-list-calls",
                "4",
                "--max-read-calls",
                "16",
            ]
        else:
            command += [
                "--checkpoint",
                item["case"]["checkpoint"],
                "--condition",
                item["condition"],
                "--access-view",
                item["access_view"],
                "--max-render-attempts",
                "3",
                "--max-model-requests",
                "12",
                "--timeout",
                "600",
            ]
            if item["condition"] == "provided":
                command += ["--provided-records", *item["case"]["provided_records"]]
        command += ["--image-condition", item["image_condition"]]
        if item["case"].get("requirements"):
            command += ["--requirements", item["case"]["requirements"]]
        identity = {
            "index": index,
            "case_id": item["case"]["id"],
            "repeat": item["repeat"],
            "condition": item["condition"],
            "access_view": item["access_view"],
            "image_condition": item["image_condition"],
        }
        print(json.dumps({"event": "starting", **identity}), flush=True)
        log_path = args.output / f"driver-{index:03}.log"
        with log_path.open("w") as log:
            try:
                exit_code = subprocess.run(
                    command, cwd=PROJECT, stdout=log, stderr=subprocess.STDOUT
                ).returncode
            except OSError as error:
                print(f"Could not start the driver: {error}", file=log)
                exit_code = None
        row = {
            **identity,
            "directory": str(run_directory),
            "exit_code": exit_code,
        }
        probe_path = run_directory / "probe.json"
        try:
            probe = json.loads(probe_path.read_text())
            if not isinstance(probe, dict) or probe.get("status") not in {
                "passed",
                "failed",
            }:
                raise ValueError("Missing terminal probe status")
            row.update(
                status=probe["status"],
                error=probe.get("error"),
                submitted_candidate=probe.get("submitted_candidate"),
                new_candidate_count=probe.get("new_candidate_count"),
                template_unchanged=probe.get("template_unchanged"),
                turn_reason=probe.get("turn_reason"),
                cleanup_error=probe.get("cleanup_error"),
            )
        except (OSError, ValueError) as error:
            row.update(
                status="driver-failed", log=str(log_path), evidence_error=str(error)
            )
        control_errors = []
        audit = run_directory / "http-audit.jsonl"
        if audit.exists():
            messages = [
                json.loads(line)
                for line in audit.read_text().splitlines()
                if line.strip()
            ]
            control_errors = [
                message
                for message in messages
                if message["type"] == "image-control-error"
                and message.get("message") != "Model request budget exceeded"
            ]
            row["model_requests"] = sum(
                bool(message.get("model")) for message in messages
            )
            row["image_control_errors"] = control_errors
        report["runs"].append(row)
        save_report(args.output / "execution.json", report)
        print(json.dumps({"event": "finished", **row}, ensure_ascii=False), flush=True)
        if (
            row["status"] == "driver-failed"
            or control_errors
            or (run_directory / "image-control-failure.json").exists()
            or row.get("template_unchanged") is False
            or row.get("cleanup_error")
        ):
            report["status"] = "needs-repair"
            break
        if (
            (row.get("error") or {})
            .get("message", "")
            .startswith("DSH session/working-directory")
        ):
            report["status"] = "needs-repair"
            break
    if report["status"] == "running":
        report["status"] = (
            "complete" if len(report["runs"]) == len(schedule) else "incomplete"
        )
    report["finished_at"] = timestamp()
    save_report(args.output / "execution.json", report)
    return 0 if report["status"] == "complete" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--phase", choices=["calibration", "diagnostic", "end-to-end"], required=True
    )
    parser.add_argument("--seed", type=int, default=20261117)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("limit must be positive")
    args.output = args.output.resolve()
    return execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
