"""Score observable recall and request visibility at target-related decisions."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


def epoch(value: str | int | float) -> float:
    if isinstance(value, (int, float)):
        return value / 1000
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def normalized_events(notifications: list[dict]) -> list[dict]:
    return [
        item["params"]["event"]
        for item in notifications
        if item.get("method") == "session.event"
    ]


def evidence_coverage(request: dict, truth: dict) -> bool:
    visible_images = {
        image["source"]["record"]
        for image in request.get("images", [])
        if image.get("source", {}).get("record") and not image.get("unresolvedFile")
    }
    visible_code = set(request.get("includedCodeSha256", []))
    visible_metadata = set(request.get("includedMetadataSha256", []))

    def item_visible(item: dict) -> bool:
        record = item["record"]
        content = item.get("content", ["image"])
        info = truth.get("records", {}).get(record, {})
        return all(
            (
                kind == "image"
                and record in visible_images
                or kind == "code"
                and info.get("code_sha256") in visible_code
                or kind == "parameters"
                and info.get("metadata_sha256") in visible_metadata
            )
            for kind in content
        )

    return any(
        all(item_visible(item) for item in group)
        for group in truth.get("acceptable_evidence_sets", [])
    )


def call_arguments(event: dict) -> dict | None:
    value = event["data"].get("arguments", {})
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def classify_reads(events: list[dict], requests: list[dict], truth: dict) -> list[dict]:
    """Classify the requested record against material available at the call."""
    records = set(truth.get("records", {}))
    records.update(
        item["record"]
        for group in truth.get("acceptable_evidence_sets", [])
        for item in group
    )
    current = truth.get("current_record")
    reference = truth.get("reference_record")
    experiment = None
    paths = dict(truth.get("path_records", {}))
    identities = dict(truth.get("candidate_records", {}))
    names = {
        e["data"]["callId"]: e["data"]["name"]
        for e in events
        if e["type"] == "tool/call"
    }
    errors = {
        e["data"]["message"]["toolCallId"]
        for e in events
        if e["type"] == "tool/result" and e["data"]["message"].get("isError")
    }
    reads = []
    for event in events:
        data = event["data"]
        if event["type"] == "tool/result":
            message = data["message"]
            name = names.get(message["toolCallId"])
            if name not in {"render_shader", "experiment_shader"} or message.get(
                "isError"
            ):
                continue
            try:
                result = json.loads(message["content"][0]["text"])
                record = result["vista"]["record"]
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            records.add(record)
            paths[result["imagePath"]] = record
            if name == "render_shader":
                current, experiment = record, None
                identities[result["id"]] = record
            else:
                experiment = record
            continue
        if event["type"] != "tool/call" or data["name"] not in {
            "mcp__vista__read_record",
            "read_image",
        }:
            continue
        args = call_arguments(event) or {}
        vista = data["name"] == "mcp__vista__read_record"
        origin = truth.get("read_origins", {}).get(data["callId"], {})
        record = args.get("record")
        if not vista:
            path = args.get("file_path")
            record = origin.get("record") or (
                paths.get(path) if isinstance(path, str) else None
            )
        if not vista and not record:
            continue
        mode = args.get("content", "image") if vista else "image"
        category = "historical"
        if (
            data["callId"] in errors
            or not isinstance(record, str)
            or mode not in ("image", "code", "both")
            or (record not in records and record != reference)
        ):
            category = "invalid"
        elif record in (reference, current, experiment) or origin.get("common"):
            category = "current-or-reference"
        else:
            preceding = [
                req for req in requests if epoch(req["time"]) <= epoch(event["time"])
            ]
            if preceding:
                request = preceding[-1]
                visible_images = {
                    img.get("source", {}).get("record")
                    or identities.get(img.get("source", {}).get("candidateId"))
                    for img in request.get("images", [])
                    if not img.get("unresolvedFile")
                    and img.get("source", {}).get("kind") != "composite"
                }
                image_visible = record in visible_images
                info = truth.get("records", {}).get(record, {})
                code_visible = info.get("code_sha256") in request.get(
                    "includedCodeSha256", []
                ) and info.get("metadata_sha256") in request.get(
                    "includedMetadataSha256", []
                )
                if (mode == "code" or image_visible) and (
                    mode == "image" or code_visible
                ):
                    category = "already-visible"
        reads.append(
            {
                "event_seq": event["seq"],
                "time": event["time"],
                "call_id": data["callId"],
                "record": record,
                "content": mode,
                "channel": "vista" if vista else "read_image",
                "classification": category,
            }
        )
    return reads


def score_trace(events: list[dict], requests: list[dict], truth: dict) -> dict:
    calls = [event for event in events if event["type"] == "tool/call"]
    annotations = {
        item["event_seq"]: item for item in truth.get("decision_annotations", [])
    }
    potential = {
        "write",
        "edit",
        "apply_patch",
        "bash",
        "render_shader",
        "select_shader_candidate",
        "submit_shader_candidate",
    }
    related = [
        event
        for event in calls
        if annotations.get(event["seq"], {}).get("classification") == "target"
    ]
    first_decision = min(related, key=lambda event: event["seq"], default=None)
    decision_seq = first_decision["seq"] if first_decision else None
    unclassified = [
        event["seq"]
        for event in calls
        if event["data"]["name"] in potential
        and annotations.get(event["seq"], {}).get("classification")
        not in {"preparation", "target"}
        and (decision_seq is None or event["seq"] < decision_seq)
    ]
    model_requests = sorted(
        (request for request in requests if request.get("model")),
        key=lambda row: epoch(row["time"]),
    )
    reads = classify_reads(events, model_requests, truth)
    recalls = [read for read in reads if read["classification"] == "historical"]
    decision_request = None
    if first_decision:
        preceding = [
            request
            for request in model_requests
            if epoch(request["time"]) <= epoch(first_decision["time"])
        ]
        decision_request = max(
            preceding, key=lambda row: epoch(row["time"]), default=None
        )
    known_timing = bool(first_decision and decision_request and not unclassified)
    trigger = None
    timely = None
    if known_timing:
        trigger = any(
            epoch(recall["time"]) < epoch(decision_request["time"])
            for recall in recalls
        )
        timely = evidence_coverage(decision_request, truth)
    decisions_with_evidence = [
        request["requestId"]
        for request in model_requests
        if evidence_coverage(request, truth)
    ]
    return {
        "status": "scored" if known_timing else "timing-unverified",
        "first_target_decision_seq": decision_seq,
        "unclassified_prior_actions": unclassified,
        "decision_request_id": decision_request.get("requestId")
        if decision_request
        else None,
        "recall_triggered_before_decision": trigger,
        "timely_related_evidence": timely,
        "requests_with_required_evidence": decisions_with_evidence,
        "recalls": recalls,
        "recall_attempted_in_run": bool(recalls),
        "required_evidence_visible_in_run": bool(decisions_with_evidence),
        "record_reads": reads,
        "invalid_argument_events": [
            event["seq"] for event in calls if call_arguments(event) is None
        ],
        "vista_read_calls": sum(
            event["data"]["name"] == "mcp__vista__read_record" for event in calls
        ),
        "vista_reads": sum(item["channel"] == "vista" for item in recalls),
        "other_image_reads": sum(item["channel"] == "read_image" for item in recalls),
        "list_calls": sum(
            event["data"]["name"] == "mcp__vista__list_records" for event in calls
        ),
        "model_requests": len(model_requests),
        "unresolved_wire_images": sum(
            bool(image.get("unresolvedFile"))
            for request in model_requests
            for image in request.get("images", [])
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run = json.loads((args.run / "probe.json").read_text())
    requests = [
        json.loads(line)
        for line in (args.run / "http-audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    result = score_trace(
        normalized_events(run["events"]), requests, json.loads(args.truth.read_text())
    )
    result["run_status"] = run["status"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "output": str(args.output)}))


if __name__ == "__main__":
    main()
