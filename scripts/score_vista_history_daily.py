"""Audit the daily-use native DSH trials offline."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from dsh_sdk_run import save_report
from run_vista_history_daily import BRANCH_REQUEST_CAP, read_wire


def load(path):
    return json.loads(path.read_text())


def successful_requests(wire):
    succeeded = {
        row["n"]
        for row in wire
        if row["kind"] == "response" and 200 <= row["status"] < 300
    }
    return [row for row in wire if row["kind"] == "request" and row["n"] in succeeded]


def input_tokens(row):
    usage = row["usage"]
    return sum(
        usage.get(name, 0)
        for name in ("inputTokens", "cacheReadTokens", "cacheWriteTokens")
    )


def text_strings(value):
    if isinstance(value, list):
        for item in value:
            yield from text_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from text_strings(item)
    elif isinstance(value, str):
        yield value


def session_log(out, session):
    logs = [
        p
        for p in (out / "home" / "sessions").rglob("session*.jsonl")
        if p.parent.name == session
    ]
    if len(logs) != 1:
        raise ValueError(f"Expected one checkpoint log in {out}, got {len(logs)}")
    return logs[0].read_bytes()


def describe_seed(out, seed):
    wire = read_wire(out)
    requests = successful_requests(wire)
    by_phase = {row["phase"]: row for row in reversed(requests)}
    normal_requests = {r["n"] for r in requests if r["purpose"] == "agent"}
    writes = {}
    for name, item in seed["writes"].items():
        request = by_phase.get(f"save-{name}", {})
        writes[name] = {
            **item,
            "request": request.get("n"),
            "png_on_wire": any(
                i.get("sha256") == item["sha256"] for i in request.get("images", [])
            ),
            "receipt_on_wire": any(
                r["short_id"] == item["short_id"] for r in request.get("receipts", [])
            ),
        }
    events = [event for turn in seed["turns"] for event in turn["events"]]
    return {
        "session": seed["session"],
        "writes": writes,
        "feedback": seed["feedback"],
        "daily_turns": sum(t["phase"].startswith("daily-") for t in seed["turns"]),
        "setup_assistant_turns": len(seed["turns"]),
        "automatic_compaction_events": [e["seq"] for e in seed["before_manual"]],
        "checkpoint_source": seed["checkpoint_source"],
        "compaction_events": [
            {"seq": e["seq"], "type": e["type"]} for e in seed["compaction"]
        ],
        "context_window": max(
            (
                e["data"].get("contextWindow", 0)
                for e in events
                if e["type"] == "request/context"
            ),
            default=0,
        ),
        "peak_input_tokens": max(
            (
                input_tokens(row)
                for row in wire
                if row["kind"] == "usage" and row.get("n") in normal_requests
            ),
            default=None,
        ),
        "visible_checkpoint_sha256": hashlib.sha256(
            session_log(out, seed["session"])
        ).hexdigest(),
    }


def tool_results(events):
    calls, errors = {}, {}
    for event in events:
        data = event["data"]
        if event["type"] == "tool/call" and data["name"] == "vista_history_read":
            raw = data["arguments"]
            args = json.loads(raw) if isinstance(raw, str) else raw
            if args.get("content", "image") in {"image", "both"}:
                calls[data["callId"]] = args.get("short_id")
        if event["type"] == "tool/result" and data["message"].get("isError"):
            message = data["message"]
            errors[message["toolCallId"]] = " ".join(
                b["text"] for b in message["content"] if b["type"] == "text"
            )
    return calls, errors


def score_case(out, case, seed, seed_log, accepted):
    report = load(out / "case.json")
    wire = read_wire(out)
    opened = [
        row for row in wire if row["kind"] == "request" and row["phase"] == case["id"]
    ]
    requests = [row for row in successful_requests(wire) if row["phase"] == case["id"]]
    initial = requests[0] if requests else {}
    turn = report.get("turn", {})
    call_ids, errors = tool_results(turn.get("events", []))
    hashes = {item["short_id"]: item["sha256"] for item in seed["writes"].values()}
    admitted = {}
    for row in requests:
        for image in row["images"]:
            call_id = image.get("toolCallId")
            short_id = call_ids.get(call_id)
            if short_id in hashes and image.get("sha256") == hashes[short_id]:
                admitted.setdefault(
                    call_id,
                    {
                        "request": row["n"],
                        "short_id": short_id,
                        "sha256": hashes[short_id],
                        "tool_call": call_id,
                    },
                )
    answers = [
        "\n".join(
            p["text"]
            for p in event["data"]["message"]["content"]
            if p["type"] == "text"
        )
        for event in turn.get("events", [])
        if event["type"] == "assistant/message"
    ]
    answer = next((text for text in reversed(answers) if text), "")
    target = seed["writes"][accepted]
    strings = list(text_strings(initial.get("body", {}).get("messages", [])))
    cues = [
        s
        for s in strings
        if target["short_id"] in s
        and any(
            word in s.lower()
            for word in ("认可", "刚好", "参照", "approved", "reference")
        )
    ]
    caps = [
        r["n"]
        for r in opened
        if r["n"] > report["starting_requests"] + BRANCH_REQUEST_CAP
    ]
    terminal = turn.get("reason", {"kind": "missing-end"})
    stop = "completed-within-budget"
    if caps:
        stop = "request-budget"
    elif terminal["kind"] != "completed":
        stop = "incomplete"
    elif any("读图额度已用完" in text for text in errors.values()):
        stop = "image-budget-rejected"
    elif len(admitted) >= 3:
        stop = "stopped-at-image-budget"
    before_target = any(
        i.get("sha256") == target["sha256"] for i in initial.get("images", [])
    )
    seen_marker = [
        s
        for s in strings
        if any(
            token.lower() in s.lower() for token in case.get("acceptable_answers", [])
        )
    ]
    relevant = {seed["writes"][name]["short_id"] for name in case["relevant"]}
    return {
        "checkpoint_prefix_verified": session_log(out, seed["session"]).startswith(
            seed_log
        ),
        "first_successful_request": initial.get("n"),
        "question_on_wire": case["question"] in strings,
        "first_model_images": initial.get("images", []),
        "target_png_in_first_request": before_target,
        "first_receipts": initial.get("retained_receipts", []),
        "first_catalog": initial.get("catalogs", []),
        "semantic_cue_candidates": [
            s for s in cues if s not in {c["text"] for c in initial.get("catalogs", [])}
        ],
        "answer_leak_candidates": seen_marker,
        "injected": [r["n"] for r in requests if r["injected"]],
        "followup_injected": [
            r["n"]
            for r in successful_requests(wire)
            if r["phase"] == "followup" and r["injected"]
        ],
        "read_attempts": list(call_ids.values()),
        "delivered": list(admitted.values()),
        "unrelated_deliveries": sum(
            a["short_id"] not in relevant for a in admitted.values()
        ),
        "errors": errors,
        "model_requests": len(requests),
        "terminal": terminal,
        "stop": stop,
        "answer": answer,
        "input_tokens": sum(
            input_tokens(r)
            for r in wire
            if r["kind"] == "usage" and r.get("n") in {q["n"] for q in requests}
        ),
    }


def summarize(folders):
    sets = {}
    for folder in folders:
        for material in load(folder / "frozen.json")["sets"]:
            root = folder / material["id"]
            if not root.exists():
                continue
            seed_path = root / "seed" / "seed.json"
            seed = load(seed_path)
            checkpoint = session_log(root / "seed", seed["session"])
            sets[material["id"]] = {
                "seed": describe_seed(root / "seed", seed),
                "cases": {
                    case["id"]: score_case(
                        root / case["id"], case, seed, checkpoint, material["accepted"]
                    )
                    for case in material["cases"]
                    if (root / case["id"] / "case.json").exists()
                },
            }
    return sets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    parser.add_argument("runs", type=Path, nargs="+")
    args = parser.parse_args()
    save_report(args.out, summarize(args.runs))


if __name__ == "__main__":
    main()
