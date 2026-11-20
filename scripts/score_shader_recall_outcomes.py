"""Summarize real recall traces and conservative, reproducible outcome checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from PIL import Image, ImageChops, ImageStat
from shader_recall_score import classify_reads, epoch, normalized_events, score_trace


def load(path: Path):
    return json.loads(path.read_text())


def json_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def source(path: Path) -> str:
    return path.read_bytes().decode("utf-8").removeprefix("\ufeff")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def defaults(code: str) -> dict:
    result = {}
    for line in code.splitlines():
        match = re.match(r"\s*//\s*param:\s*(\w+)\s*\|", line)
        value = re.search(r"\bdefault:\s*([^|]+)", line)
        if match and value:
            raw = value[1].strip()
            try:
                parsed = json.loads(raw)
            except ValueError:
                try:
                    parsed = float(raw)
                except ValueError:
                    parsed = raw
            result[match[1]] = parsed
    return result


def body_tokens(code: str) -> list[str]:
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", code, flags=re.S)
    return re.findall(
        r"[A-Za-z_]\w*|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|"
        r"\+\+|--|==|!=|<=|>=|\+=|-=|\*=|/=|&&|\|\||<<|>>|[^\s]",
        code,
    )


def code_equivalent(before: str, after: str) -> bool:
    return body_tokens(before) == body_tokens(after) and defaults(before) == defaults(
        after
    )


def run_cost(probe: dict) -> dict:
    events = normalized_events(probe.get("events", []))
    usage = Counter()
    tool_calls = []
    for event in events:
        if event["type"] == "assistant/message":
            usage.update(event["data"].get("usage", {}))
        elif event["type"] == "tool/call":
            data = event["data"]
            tool_calls.append((data["name"], str(data.get("arguments", ""))))
    repeats = Counter(tool_calls)
    return {
        "wall_seconds": epoch(probe["finished_at"]) - epoch(probe["started_at"]),
        "tool_calls": len(tool_calls),
        "tool_calls_by_name": dict(Counter(name for name, _args in tool_calls)),
        "repeated_identical_tool_calls": sum(count - 1 for count in repeats.values()),
        "usage": dict(usage),
        "currency_cost": None,
        "currency_cost_note": (
            "Account billing amount is not exposed in the captured session events."
        ),
    }


def generation_trace(run_root: Path, probe: dict) -> dict:
    events = normalized_events(probe.get("events", []))
    requests = [
        row for row in json_lines(run_root / "http-audit.jsonl") if row.get("model")
    ]
    image_reads = json_lines(run_root / "tool-image-control.jsonl")
    reference = (probe.get("restored") or {}).get("referenceRecord", {})
    reads = classify_reads(
        events,
        requests,
        {
            "reference_record": reference.get("record"),
            "read_origins": {row["callId"]: row["origin"] for row in image_reads},
        },
    )
    historical = [read for read in reads if read["classification"] == "historical"]
    composite_reads = [
        row
        for row in image_reads
        if row["origin"]["kind"] == "composite"
        and any(not region.get("common") for region in row["origin"]["regions"])
    ]
    return {
        "status": "generation-observations-without-necessity-annotation",
        "recall_attempted_in_run": bool(historical),
        "vista_reads": sum(row["channel"] == "vista" for row in historical),
        "other_image_reads": sum(row["channel"] == "read_image" for row in historical),
        "list_calls": sum(
            event["type"] == "tool/call"
            and event["data"]["name"] == "mcp__vista__list_records"
            for event in events
        ),
        "model_requests": len(requests),
        "explicit_historical_reads": historical,
        "record_reads": reads,
        "vista_read_calls": sum(row["channel"] == "vista" for row in reads),
        "comparison_reads_containing_history": len(composite_reads),
        "earlier_candidate_available_transitions": max(
            0, probe.get("new_candidate_count", 0) - 1
        ),
        "necessary_recall_opportunities": None,
        "unresolved_wire_images": sum(
            bool(image.get("unresolvedFile"))
            for request in requests
            for image in request.get("images", [])
        ),
    }


def evidence_truth(checkpoint: dict, records: list[str], run_root: Path) -> dict:
    info = {}
    path_records = {}
    for candidate_id, entry in checkpoint["mapping"].items():
        record = entry["record"]
        raw = load(run_root / "vista/json" / record)
        code = source(run_root / "vista" / raw["glsl"]) if raw["glsl"] else None
        metadata = {
            "record": record,
            "label": raw["label"],
            "source_agent": raw["source_agent"],
        }
        if "render" in raw:
            metadata["render"] = raw["render"]
        info[record] = {
            "code_sha256": digest(code) if code is not None else None,
            "metadata_sha256": digest(json.dumps(metadata, ensure_ascii=False)),
        }
        path_records[
            str(
                run_root
                / "business/restore-probe/candidates"
                / candidate_id
                / "render.png"
            )
        ] = record
    groups = []
    for record in records:
        groups.append([{"record": record, "content": ["image"]}])
        groups.append([{"record": record, "content": ["code", "parameters"]}])
    return {
        "current_record": checkpoint["mapping"][checkpoint["currentCandidateId"]][
            "record"
        ],
        "reference_record": checkpoint["referenceRecord"],
        "candidate_records": {
            cid: row["record"] for cid, row in checkpoint["mapping"].items()
        },
        "read_origins": {
            row["callId"]: row["origin"]
            for row in json_lines(run_root / "tool-image-control.jsonl")
        },
        "records": info,
        "path_records": path_records,
        "acceptable_evidence_sets": groups,
    }


def decision_annotations(
    events: list[dict], run_root: Path, checkpoint: dict
) -> list[dict]:
    edits = {row["callId"]: row for row in json_lines(run_root / "source-edits.jsonl")}
    result = []
    for event in events:
        if event["type"] != "tool/call":
            continue
        data = event["data"]
        name = data["name"]
        if name in {"select_shader_candidate", "submit_shader_candidate"}:
            result.append(
                {
                    "event_seq": event["seq"],
                    "classification": "target",
                    "method": "candidate-choice",
                }
            )
        elif data["callId"] in edits:
            edit = edits[data["callId"]]
            classification = "preparation"
            method = "working-source-unchanged"
            if edit["changed"]:
                before = (
                    source(Path(edit["beforePath"])) if edit.get("beforePath") else ""
                )
                after = source(Path(edit["afterPath"])) if edit.get("afterPath") else ""
                if code_equivalent(before, after):
                    method = "format-and-metadata-equivalent"
                elif body_tokens(before) == body_tokens(after):
                    classification = "target"
                    method = "effective-default-change"
                else:
                    classification = "unknown"
                    method = "semantic-or-rendered-region-review-required"
            result.append(
                {
                    "event_seq": event["seq"],
                    "classification": classification,
                    "method": method,
                }
            )
        elif name == "render_shader":
            # A formal replay is treated conservatively. The source-edit audit
            # identifies earlier changes; this event still needs a region check
            # when the model renders a separate source path.
            args = json.loads(data.get("arguments", "{}"))
            path = args.get("filePath")
            workspace = run_root / "workspace/stages/author/tasks/restore-probe"
            if not path or Path(path) == workspace / "shader.frag":
                result.append(
                    {
                        "event_seq": event["seq"],
                        "classification": "preparation",
                        "method": "main-source-render-after-audited-edits",
                    }
                )
    return result


def outcome(probe: dict, checkpoint: dict, case: dict) -> dict:
    result = {
        "author_submission_completed": probe["status"] == "passed",
        "visual_acceptance": "pending-review",
    }
    path = probe.get("submitted_source_path")
    image_path = probe.get("submitted_image_path")
    if not path and probe.get("new_candidate_count", 0):
        directory = Path(probe["restored"]["directory"])
        task = load(directory / "task.json")
        candidate_id = task["workflow"]["rounds"][-1]["candidateIds"][-1]
        path = str(directory / "candidates" / candidate_id / "shader.frag")
        image_path = str(directory / "candidates" / candidate_id / "render.png")
        result["produced_candidate_id"] = candidate_id
        result["candidate_state"] = "produced-without-submission"
    elif path:
        result["candidate_state"] = "submitted"
    else:
        result["candidate_state"] = "no-formal-candidate"
    if not path or not Path(path).exists():
        return result
    result["evaluated_source_path"] = path
    result["evaluated_image_path"] = image_path
    baseline = source(
        Path(checkpoint["mapping"][checkpoint["currentCandidateId"]]["sourcePath"])
    )
    actual = source(Path(path))
    objective = case.get("objective")
    if not objective:
        match = re.search(
            r"将当前 Shader 中 (\w+) 的默认值设为 ([0-9.]+)",
            case.get("requirements", ""),
        )
        if match:
            objective = {
                "type": "single-default-change",
                "parameter": match[1],
                "value": float(match[2]),
            }
    if objective and objective["type"] == "single-default-change":
        before = defaults(baseline)
        after = defaults(actual)
        parameter = objective["parameter"]
        target = after.get(parameter)
        target_ok = (
            isinstance(target, (int, float))
            and abs(target - objective["value"]) <= 1e-6
        )
        other_ok = {k: v for k, v in before.items() if k != parameter} == {
            k: v for k, v in after.items() if k != parameter
        }
        body_ok = body_tokens(actual) == body_tokens(baseline)
        result.update(
            target_default_correct=target_ok,
            other_defaults_preserved=other_ok,
            body_tokens_preserved=body_ok,
            functional_objective_met=bool(target_ok and other_ok and body_ok),
        )
    reference = Path(checkpoint["businessDirectory"]) / "reference.png"
    initial = Path(checkpoint["mapping"][checkpoint["currentCandidateId"]]["imagePath"])
    if image_path:
        with (
            Image.open(reference) as reference_image,
            Image.open(initial) as baseline_image,
            Image.open(image_path) as actual_image,
        ):
            if (
                reference_image.size != actual_image.size
                or reference_image.size != baseline_image.size
            ):
                raise ValueError("Outcome images have different dimensions")
            for label, image in [("before", baseline_image), ("after", actual_image)]:
                difference = ImageChops.difference(
                    reference_image.convert("RGB"), image.convert("RGB")
                )
                result[f"pixel_mae_{label}"] = sum(ImageStat.Stat(difference).mean) / 3
            result["pixel_metric_note"] = (
                "Auxiliary full-image pixel difference. Visual acceptance needs review."
            )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    args = parser.parse_args()
    execution = load(args.batch / "execution.json")
    schedule = load(args.batch / "schedule.json")
    scored = []
    for row, scheduled in zip(execution["runs"], schedule, strict=False):
        run_root = Path(row["directory"])
        if not (run_root / "probe.json").exists():
            scored.append({**row, "scoring_status": "missing-driver-evidence"})
            continue
        probe = load(run_root / "probe.json")
        case = scheduled["case"]
        if not case.get("checkpoint"):
            scored.append(
                {
                    **row,
                    "outcome": {
                        "author_submission_completed": probe["status"] == "passed",
                        "visual_acceptance": "pending-review",
                    },
                    "cost": run_cost(probe),
                    "trace": generation_trace(run_root, probe),
                }
            )
            continue
        checkpoint = load(Path(case["checkpoint"]))
        events = normalized_events(probe.get("events", []))
        truth = evidence_truth(
            checkpoint,
            case.get("acceptable_records", case["provided_records"]),
            run_root,
        )
        truth["decision_annotations"] = decision_annotations(
            events, run_root, checkpoint
        )
        trace = score_trace(events, json_lines(run_root / "http-audit.jsonl"), truth)
        if case["purpose"] == "information-sufficient":
            trace["recall_necessity"] = "not-required"
            trace["timely_related_evidence"] = None
            trace["recall_triggered_before_decision"] = None
        detail = {
            **row,
            "trace": trace,
            "outcome": outcome(probe, checkpoint, case),
            "cost": run_cost(probe),
            "scoring_status": "complete-with-visual-review-pending",
        }
        (run_root / "score.json").write_text(
            json.dumps(detail, ensure_ascii=False, indent=2) + "\n"
        )
        scored.append(detail)
    summary = {
        "execution_status": execution["status"],
        "runs": len(scored),
        "planned_runs": execution.get("requested_runs", len(schedule)),
        "original_scheduled_runs": len(schedule),
        "scope_execution_status": "completed"
        if len(scored) == execution.get("requested_runs", len(schedule))
        else "partial",
        "author_submissions_completed": sum(
            row.get("outcome", {}).get("author_submission_completed", False)
            for row in scored
        ),
        "run_status_counts": dict(
            Counter(row.get("status", "unknown") for row in scored)
        ),
        "results": scored,
    }
    (args.batch / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {key: value for key, value in summary.items() if key != "results"},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
