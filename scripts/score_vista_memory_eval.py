"""Score frozen VISTA reasoning cases and audit paired DSH run evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import random
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean

from PIL import Image
from vista_eval_data import image_metric, pixel_error, render_case
from vista_eval_paths import load_dataset

TOOLS = {"mcp__vista__list_records", "mcp__vista__read_record"}
FIELDS = {"R1": ("record",), "R2": ("pair", "direction"), "R3": ("action",)}


def load(path: Path):
    return json.loads(path.read_text())


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def json_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def timestamp(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def parse_answer(text: str) -> dict:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines[-1].strip() != "```":
            raise ValueError("Unclosed JSON fence")
        value = "\n".join(lines[1:-1])
    answer = json.loads(value)
    if not isinstance(answer, dict):
        raise ValueError("Answer must be one JSON object")
    return answer


def input_identity(executed: dict) -> dict:
    """Read input identity before grading, including for interrupted turns."""
    canonical = []
    for item in executed.get("events", []):
        params = item.get("params", {})
        event = params.get("event", {})
        if (
            item.get("method") != "session.event"
            or params.get("sessionId") != executed.get("session_id")
            or event.get("type") != "user/message"
        ):
            continue
        images = [
            block["attachment"]["attachmentId"].removeprefix("sha256:")
            for block in event.get("data", {}).get("content", [])
            if block.get("type") == "image"
        ]
        if images:
            canonical = images
            break
    return {
        "prompt_sha256": hashlib.sha256(
            executed.get("prompt", "").encode()
        ).hexdigest(),
        "common_images": [item["sha256"] for item in executed.get("prompt_images", [])],
        "canonical_common_images": canonical,
    }


def failure_stage(run: dict, executed: dict) -> str:
    """Prefer explicit runner stages and support existing recorded runs."""
    explicit = run.get("fatal_error", {}).get("stage")
    if explicit:
        return explicit
    reason = (executed.get("turn_end") or {}).get("data", {}).get("reason", {})
    if reason.get("kind") == "max-tokens":
        return "task"
    if "initialize_response" not in run:
        return "initialize"
    if "wait_response" in executed or executed.get("status") == "completed":
        return "shutdown"
    if executed.get("started_at"):
        return "task"
    return "initialize"


def audit_run(folder: Path, metadata: dict, run: dict, case: dict, truth: dict) -> dict:
    """Validate the information actually delivered in this completed run."""
    observations = []

    def check(name: str, passed: bool) -> None:
        observations.append({"name": name, "passed": bool(passed)})

    executed = run["cases"][0]
    notifications = executed.get("events", [])
    events = [
        item["params"]["event"]
        for item in notifications
        if item.get("method") == "session.event"
        and item.get("params", {}).get("sessionId") == executed["session_id"]
    ]
    requests = []
    pending = {}
    for item in json_lines(folder / "http-audit.jsonl"):
        if item["type"] == "request":
            check("Unique outstanding HTTP request", item["requestId"] not in pending)
            requests.append(item)
            pending[item["requestId"]] = item
        elif item["type"] in {"response", "transport-error"}:
            request = pending.pop(item["requestId"], None)
            check("HTTP response has a request", request is not None)
            if request is not None:
                request["response"] = item
    check("Actual HTTP requests exist", bool(requests))
    check("All HTTP requests settled", not pending)
    common = Counter([truth["target_sha256"], truth["current_sha256"]])
    check(
        "Archived common images match frozen truth",
        hashlib.sha256(Path(case["target_path"]).read_bytes()).hexdigest()
        == truth["target_sha256"]
        and hashlib.sha256(Path(case["current_path"]).read_bytes()).hexdigest()
        == truth["current_sha256"],
    )
    prompt_images = executed.get("prompt_images", [])
    check(
        "Common images match source hashes",
        Counter(item["sha256"] for item in prompt_images) == common,
    )
    user_images = []
    for event in events:
        if event["type"] != "user/message":
            continue
        content = event.get("data", {}).get("content", [])
        images = [block["attachment"] for block in content if block["type"] == "image"]
        if images:
            user_images = images
            break
    check("Canonical user input has two images", len(user_images) == 2)
    canonical_common = Counter()
    image_mapping = []
    for index, attachment in enumerate(user_images[:2]):
        role = ("target", "current")[index]
        digest = attachment["attachmentId"].removeprefix("sha256:")
        canonical_common[digest] += 1
        stored = Path(run["home"]) / "attachments/v1/objects" / digest[:2] / digest
        suffix = ".jpg" if attachment["mediaType"] == "image/jpeg" else ".png"
        archive = folder / f"common-{role}{suffix}"
        data = archive.read_bytes() if archive.exists() else stored.read_bytes()
        check(
            "Canonical attachment bytes match hash",
            hashlib.sha256(data).hexdigest() == digest,
        )
        if not archive.exists():
            archive.write_bytes(data)
        with (
            Image.open(io.BytesIO(data)) as canonical,
            Image.open(case[f"{role}_path"]) as original,
        ):
            dimensions_match = (
                canonical.size == original.size == (case["width"], case["height"])
            )
            check("Canonical image dimensions match source", dimensions_match)
            error = (
                pixel_error(canonical.convert("RGB"), original.convert("RGB"))
                if dimensions_match
                else None
            )
        check(
            "Canonical conversion preserves pixels within JPEG tolerance",
            error is not None and error <= 5.0,
        )
        image_mapping.append(
            {
                "role": role,
                "source_sha256": truth[f"{role}_sha256"],
                "canonical_sha256": digest,
                "media_type": attachment["mediaType"],
                "width": attachment["width"],
                "height": attachment["height"],
                "pixel_mae": error,
                "pixel_mae_tolerance": 5.0,
                "archived_path": str(archive),
            }
        )
    check("Prompt matches frozen case", executed.get("prompt") == case["prompt"])
    check(
        "Initialization fixes model and reasoning",
        run["initialize_params"].get("provider") == "openai"
        and run["initialize_params"].get("model") == "gpt-6.1-sol"
        and run["initialize_params"].get("reasoningEffort") == "high"
        and run["initialize_params"].get("maxTokens") == 8192,
    )
    history_hashes = set(truth["png_sha256"].values())
    if case.get("encoding_revision", 1) >= 2:
        check(
            "Common source hashes cannot identify history records",
            not set(common).intersection(history_hashes),
        )
        check(
            "Canonical common hashes cannot identify history records",
            not set(canonical_common).intersection(history_hashes),
        )
    for request in requests:
        check("HTTP status is 200", request.get("response", {}).get("status") == 200)
        check("Only VISTA HTTP tools", set(request.get("tools", [])) == TOOLS)
        check("HTTP model", request.get("model") == "gpt-6.1-sol")
        efforts = request.get("configurationReasoningEfforts", [])
        check("HTTP reasoning update is high", bool(efforts) and efforts[-1] == "high")
        images = request.get("images", [])
        if case.get("encoding_revision", 1) >= 2:
            check(
                "Common HTTP hashes cannot identify history records",
                all(
                    image["sha256"] not in history_hashes
                    for image in images
                    if image.get("originRole") == "user"
                ),
            )
        check(
            "HTTP common image identities and multiplicity",
            Counter(
                image["sha256"] for image in images if image.get("originRole") == "user"
            )
            == canonical_common,
        )
        historical = [
            image
            for image in images
            if image.get("originType") == "function_call_output"
        ]
        check(
            "HTTP image origins are attributable",
            all(
                image.get("originRole") == "user"
                or image.get("originType") == "function_call_output"
                for image in images
            ),
        )
        if metadata["condition"] == "text":
            check("Text condition has no HTTP history images", not historical)
        else:
            check(
                "HTTP history images are known records",
                all(image["sha256"] in history_hashes for image in historical),
            )

    calls = Counter()
    call_by_id = {}
    history_reads = 0
    tool_errors = 0
    usage = Counter()
    final_text = ""
    for event in events:
        data = event.get("data", {})
        if event["type"] == "tool/call":
            calls[data["name"]] += 1
            call_by_id[data["callId"]] = data
            check("Only VISTA called tools", data["name"] in TOOLS)
        elif event["type"] == "tool/result":
            message = data["message"]
            call = call_by_id.get(message["toolCallId"])
            check("Tool result matches a call", call is not None)
            images = [block for block in message["content"] if block["type"] == "image"]
            if message.get("isError"):
                tool_errors += 1
            if metadata["condition"] == "text":
                check("Text condition has no tool images", not images)
            for block in images:
                history_reads += 1
                attachment = block["attachment"]
                image_hash = attachment["attachmentId"].removeprefix("sha256:")
                record = json.loads(call["arguments"]).get("record") if call else None
                check(
                    "Tool image matches requested record",
                    truth["png_sha256"].get(record) == image_hash,
                )
                check(
                    "Tool image reaches a later HTTP request",
                    any(
                        image["sha256"] == image_hash
                        and image.get("originType") == "function_call_output"
                        for request in requests
                        if timestamp(request["time"]) >= event["time"] / 1000
                        for image in request.get("images", [])
                    ),
                )
        elif event["type"] == "assistant/message":
            usage.update(data.get("usage", {}))
            text = "\n".join(
                block["text"]
                for block in data["message"]["content"]
                if block["type"] == "text"
            )
            if text:
                final_text = text
    mcp_events = json_lines(folder / "mcp-audit.jsonl")
    check("MCP log covers each model tool call", len(mcp_events) == sum(calls.values()))
    check(
        "MCP log records expected condition",
        all(item["condition"] == metadata["condition"] for item in mcp_events),
    )
    elapsed = timestamp(executed["finished_at"]) - timestamp(executed["started_at"])
    budget_ok = (
        calls["mcp__vista__list_records"] <= 2
        and calls["mcp__vista__read_record"] <= 8
        and elapsed <= 300.5
        and all(item.get("allowed", False) for item in mcp_events)
    )
    return {
        "valid": all(item["passed"] for item in observations),
        "checks": observations,
        "budget_ok": budget_ok,
        "tool_calls": dict(calls),
        "history_image_reads": history_reads,
        "tool_errors": tool_errors,
        "http_requests": len(requests),
        "wall_seconds": elapsed,
        "usage": dict(usage),
        "final_text": final_text,
        "common_images": sorted(common.elements()),
        "canonical_common_images": list(
            attachment["attachmentId"].removeprefix("sha256:")
            for attachment in user_images
        ),
        "common_image_mapping": image_mapping,
    }


def score_run(folder: Path, case: dict, truth: dict | None = None) -> dict:
    metadata = load(folder / "metadata.json")
    result = {
        **metadata,
        "task_type": case["task_type"],
        "scene_family": case["scene_family"],
        "caption_detail": case["caption_detail"],
        "status": "pending",
        "success": False,
        "folder": str(folder),
    }
    path = folder / "run.json"
    if not path.exists():
        return result
    run = load(path)
    if run.get("status") == "running":
        return result
    executed = (run.get("cases") or [{}])[0]
    usage = Counter()
    for item in executed.get("events", []):
        event = item.get("params", {}).get("event", {})
        if event.get("type") == "assistant/message":
            usage.update(event["data"].get("usage", {}))
    result["usage"] = dict(usage)
    result["input_identity"] = input_identity(executed)
    if run.get("status") != "completed":
        reason = (executed.get("turn_end") or {}).get("data", {}).get("reason", {})
        timeout = run.get("fatal_error", {}).get("type") == "TimeoutError"
        stage = failure_stage(run, executed)
        task_timeout = timeout and stage in {"task", "prompt", "wait"}
        result["status"] = (
            "failed" if task_timeout or reason.get("kind") == "max-tokens" else "infra"
        )
        result["failure_stage"] = stage
        result["failure"] = run.get("fatal_error", reason)
        return result
    if truth is None:
        truth = verified_truth(case)
    audit = audit_run(folder, metadata, run, case, truth)
    result["audit"] = audit
    if not audit["valid"]:
        result["status"] = "infra"
        result["failure"] = "Evidence or condition validation failed"
        return result
    if not audit["budget_ok"]:
        result["status"] = "failed"
        result["failure"] = "Predeclared execution budget exceeded"
        return result
    try:
        answer = parse_answer(audit["final_text"])
    except (ValueError, TypeError) as error:
        result["status"] = "failed"
        result["failure"] = f"Answer format: {error}"
        return result
    result["answer"] = answer
    result["answer_fields_correct"] = {
        field: answer.get(field) == truth["answer"][field]
        for field in FIELDS[case["task_type"]]
    }
    evidence = answer.get("evidence_records", [])
    evidence_valid = isinstance(evidence, list) and all(
        isinstance(item, str) for item in evidence
    )
    evidence_set = set(evidence) if evidence_valid else set()
    result["evidence_correct"] = (
        evidence_valid
        and evidence_set <= set(case["record_order"])
        and any(set(required) <= evidence_set for required in truth["evidence_sets"])
    )
    result["optional_measurements"] = {
        "reported": answer.get("evidence"),
        "expected": truth["answer"].get("evidence"),
        "used_for_primary_score": False,
    }
    action_ok = True
    if case["task_type"] == "R3":
        action = answer.get("action")
        action_ok = isinstance(action, str) and action in case["action_options"]
        if action_ok:
            image = render_case(case, case["action_options"][action])
            image.save(folder / "action.png")
            with Image.open(case["target_path"]) as target:
                error = pixel_error(image, target.convert("RGB"))
            best = min(truth["action_errors"].values())
            action_ok = math.isclose(error, best, rel_tol=0, abs_tol=1e-9)
            result["action_execution"] = {
                "action": action,
                "control": case["action_options"][action],
                "pixel_error": error,
                "optimal_pixel_error": best,
                "measured_feature": image_metric(case, image),
                "image_path": str(folder / "action.png"),
            }
    result["success"] = (
        all(result["answer_fields_correct"].values())
        and result["evidence_correct"]
        and action_ok
    )
    result["status"] = "passed" if result["success"] else "failed"
    return result


def interval(values: list[float], seed: int = 20261116) -> list[float] | None:
    if not values:
        return None
    rng = random.Random(seed)
    samples = sorted(mean(rng.choices(values, k=len(values))) for _ in range(10000))
    return [samples[249], samples[9749]]


def summarize(case_rows: list[dict]) -> dict:
    if not case_rows:
        return {"case_count": 0}
    differences = [row["difference"] for row in case_rows]
    return {
        "case_count": len(case_rows),
        "text_success_rate": mean(row["text_success_rate"] for row in case_rows),
        "visual_success_rate": mean(row["visual_success_rate"] for row in case_rows),
        "paired_difference": mean(differences),
        "bootstrap_95_percent_interval": interval(differences),
    }


def verified_truth(case: dict) -> dict:
    truth = load(Path(case["truth_path"]))
    if truth["case_id"] != case["id"]:
        raise ValueError(f"Truth does not match {case['id']}")
    if not set(FIELDS[case["task_type"]]) <= truth["answer"].keys():
        raise ValueError(f"Incomplete truth answer for {case['id']}")
    if not truth["evidence_sets"] or any(
        not group or not set(group) <= set(case["record_order"])
        for group in truth["evidence_sets"]
    ):
        raise ValueError(f"Invalid evidence sets for {case['id']}")
    if case["task_type"] == "R3" and set(truth["action_errors"]) != set(
        case["action_options"]
    ):
        raise ValueError(f"Missing action errors for {case['id']}")
    for role in ("current", "target"):
        image = Path(case[f"{role}_path"])
        if hashlib.sha256(image.read_bytes()).hexdigest() != truth[f"{role}_sha256"]:
            raise ValueError(f"Frozen image changed: {image}")
    root = Path(case["root"])
    for record in case["record_order"]:
        raw = load(root / "json" / record)
        if (
            hashlib.sha256((root / raw["png"]).read_bytes()).hexdigest()
            != truth["png_sha256"][record]
        ):
            raise ValueError(f"Frozen history image changed: {record}")
    return truth


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=("calibration", "formal"), required=True)
    args = parser.parse_args()
    dataset = load_dataset(args.dataset)
    cases = {
        case["id"]: {**case, "encoding_revision": dataset.get("encoding_revision", 1)}
        for case in dataset["cases"]
        if case["split"] == args.phase
    }
    phase = args.output / args.phase
    if not cases:
        parser.error("No cases selected for this phase")
    truths = {case_id: verified_truth(case) for case_id, case in cases.items()}
    scores = []
    for path in sorted((phase / "runs").glob("*/metadata.json")):
        metadata = load(path)
        case = cases[metadata["case_id"]]
        try:
            score = score_run(path.parent, case, truths[case["id"]])
        except (ValueError, TypeError, KeyError, IndexError) as error:
            score = {
                **metadata,
                "status": "infra",
                "success": False,
                "failure": str(error),
                "folder": str(path.parent),
            }
        scores.append(score)
    for score in scores:
        save(Path(score["folder"]) / "score.json", score)
    grouped = defaultdict(dict)
    for score in scores:
        grouped[(score["case_id"], score["repeat"], score["attempt"])][
            score["condition"]
        ] = score
    repeats = (1,) if args.phase == "calibration" else (1, 2)
    pairs = []
    missing = []
    for case_id in cases:
        for repeat in repeats:
            eligible = []
            for (identifier, repetition, attempt), conditions in grouped.items():
                if (
                    identifier != case_id
                    or repetition != repeat
                    or set(conditions) != {"text", "visual"}
                ):
                    continue
                if any(
                    score["status"] in {"infra", "pending"}
                    for score in conditions.values()
                ):
                    continue
                text, visual = conditions["text"], conditions["visual"]
                same_input = (
                    text.get("input_identity") == visual.get("input_identity")
                    and len(text.get("input_identity", {}).get("common_images", []))
                    == 2
                    and len(
                        text.get("input_identity", {}).get(
                            "canonical_common_images", []
                        )
                    )
                    == 2
                )
                if same_input:
                    eligible.append((attempt, conditions))
            if not eligible:
                missing.append({"case_id": case_id, "repeat": repeat})
                continue
            attempt, conditions = max(eligible, key=lambda item: item[0])
            pairs.append(
                {
                    "case_id": case_id,
                    "repeat": repeat,
                    "attempt": attempt,
                    "text": conditions["text"],
                    "visual": conditions["visual"],
                }
            )
    case_rows = []
    for case_id, case in cases.items():
        selected = [pair for pair in pairs if pair["case_id"] == case_id]
        if len(selected) != len(repeats):
            continue
        text_rate = mean(pair["text"]["success"] for pair in selected)
        visual_rate = mean(pair["visual"]["success"] for pair in selected)
        case_rows.append(
            {
                "case_id": case_id,
                "task_type": case["task_type"],
                "scene_family": case["scene_family"],
                "caption_detail": case["caption_detail"],
                "scenario": case["scenario"],
                "text_success_rate": text_rate,
                "visual_success_rate": visual_rate,
                "difference": visual_rate - text_rate,
            }
        )
    total_usage = Counter()
    for score in scores:
        total_usage.update(score.get("usage", {}))
    usage_by_condition = {}
    for condition in ("text", "visual"):
        selected_runs = [pair[condition] for pair in pairs]
        usage = Counter()
        for score in selected_runs:
            usage.update(score.get("usage", {}))
        durations = [
            score["audit"]["wall_seconds"]
            for score in selected_runs
            if "audit" in score
        ]
        usage_by_condition[condition] = {
            "runs": len(selected_runs),
            "usage": dict(usage),
            "mean_wall_seconds": mean(durations) if durations else None,
            "history_image_reads": sum(
                score.get("audit", {}).get("history_image_reads", 0)
                for score in selected_runs
            ),
            "tool_calls": sum(
                sum(score.get("audit", {}).get("tool_calls", {}).values())
                for score in selected_runs
            ),
        }
    families = sorted({case["scene_family"] for case in cases.values()})
    family_differences = [
        mean(row["difference"] for row in case_rows if row["scene_family"] == family)
        for family in families
        if any(row["scene_family"] == family for row in case_rows)
    ]
    report = {
        "status": "pending" if missing else "complete",
        "phase": args.phase,
        "expected_primary_runs": len(cases) * len(repeats) * 2,
        "all_attempt_counts": dict(Counter(score["status"] for score in scores)),
        "all_attempt_success_rate": mean(
            score["success"] for score in scores if score["status"] != "pending"
        )
        if any(score["status"] != "pending" for score in scores)
        else None,
        "missing_valid_pairs": missing,
        "overall": summarize(case_rows),
        "by_task": {
            task: summarize([row for row in case_rows if row["task_type"] == task])
            for task in sorted({case["task_type"] for case in cases.values()})
        },
        "by_caption_detail": {
            detail: summarize(
                [row for row in case_rows if row["caption_detail"] == detail]
            )
            for detail in ("detailed", "coarse")
        },
        "by_scene_family": {
            family: summarize(
                [row for row in case_rows if row["scene_family"] == family]
            )
            for family in sorted({case["scene_family"] for case in cases.values()})
        },
        "by_context_scenario": {
            scenario: summarize(
                [row for row in case_rows if row["scenario"] == scenario]
            )
            for scenario in sorted({case["scenario"] for case in cases.values()})
        },
        "family_block_sensitivity": {
            "family_count": len(family_differences),
            "mean_difference": mean(family_differences) if family_differences else None,
            "bootstrap_95_percent_interval": interval(family_differences),
        },
        "all_attempt_usage": dict(total_usage),
        "selected_usage_by_condition": usage_by_condition,
        "currency_cost": None,
        "currency_cost_note": "Monetary charges require provider billing data.",
        "cases": case_rows,
        "selected_pairs": [
            {
                "case_id": pair["case_id"],
                "repeat": pair["repeat"],
                "attempt": pair["attempt"],
                "text_run": pair["text"]["run_id"],
                "visual_run": pair["visual"]["run_id"],
            }
            for pair in pairs
        ],
        "all_run_scores": [
            {
                "run_id": score["run_id"],
                "status": score["status"],
                "success": score["success"],
                "score_path": str(Path(score["folder"]) / "score.json"),
            }
            for score in scores
        ],
    }
    save(phase / "paired-summary.json", report)
    with (phase / "paired-summary.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "case_id",
                "task_type",
                "scene_family",
                "caption_detail",
                "scenario",
                "text_success_rate",
                "visual_success_rate",
                "difference",
            ],
        )
        writer.writeheader()
        writer.writerows(case_rows)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "expected_primary_runs",
                    "all_attempt_counts",
                    "overall",
                )
            },
            ensure_ascii=False,
        )
    )
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
