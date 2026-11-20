"""Verify the native DSH history probe without new model requests."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from dsh_sdk_run import save_report

CASES = (
    "fixed-query",
    "fixed-auto",
    "fixed-sequential",
    "controlled",
    "simultaneous",
    "native",
    "offload",
    "notes",
)
FIXED_TEXT = "R2 · 用户认可过这版留白"
TEMP_PATH = re.compile(
    r'/(?:private/)?var/folders/[^\s"\)]+/vista-history-probe-[^/"\s]+'
)


def read_run(path):
    events = json.loads((path / "events.json").read_text())
    assert events["complete"]
    rows = [json.loads(line) for line in (path / "wire.jsonl").read_text().splitlines()]
    status = {row["n"]: row["status"] for row in rows if row["kind"] == "response"}
    requests = [row for row in rows if row["kind"] == "request"]
    assert all(status.get(row["n"]) == 200 for row in requests)
    return {
        "target": events["target_sha256"],
        "events": [
            row["params"]["event"]
            for row in events["notifications"]
            if row["method"] == "session.event"
        ],
        "notes": events.get("notes", []),
        "requests": requests,
        "files": {
            row["fileId"]: row["sha256"] for row in rows if row["kind"] == "upload"
        },
        "tools": [row for row in rows if row["kind"] == "tool"],
        "usage": {row["n"]: row["usage"] for row in rows if row["kind"] == "usage"},
        "writes": [row for row in rows if row["kind"] in {"save", "user-feedback"}],
    }


def first(run, phase):
    return next(row for row in run["requests"] if row["phase"] == phase)


def visible(run, request):
    return any(image.get("sha256") == run["target"] for image in request["images"])


def canonical_body(value, files):
    if isinstance(value, list):
        return [
            canonical_body(item, files)
            for item in value
            if not isinstance(item, dict) or item.get("text") != FIXED_TEXT
        ]
    if isinstance(value, dict):
        return {
            key: files[item] if key == "file_id" else canonical_body(item, files)
            for key, item in value.items()
        }
    if isinstance(value, str):
        return TEMP_PATH.sub("<temp>", value)
    return value


def response_text(run):
    answer = next(
        event
        for event in reversed(run["events"])
        if event["type"] == "assistant/message"
    )
    return "\n".join(
        part["text"]
        for part in answer["data"]["message"]["content"]
        if part["type"] == "text"
    )


def verify(runs):
    pair = {}
    for name, injection in (("fixed-query", 0), ("fixed-auto", 1)):
        run = runs[name]
        opening = first(run, "feedback")
        assert opening["injected"] == injection and not visible(run, opening)
        assert len(opening["catalogs"]) == injection
        assert visible(run, first(run, "history"))
        assert not opening["receipts"]
        requested = [row for row in run["tools"] if row["name"] == "vista_history_read"]
        received = [
            (row["n"], image.get("toolCallId"))
            for row in run["requests"]
            if row["phase"] == "feedback"
            for image in row["images"]
            if image.get("sha256") == run["target"]
        ]
        assert requested and requested[0]["args"]["short_id"] == "R2"
        assert received and received[0][1] == requested[0]["call_id"]
        body = canonical_body(opening["body"], run["files"])
        body_hash = hashlib.sha256(
            json.dumps(body, sort_keys=True).encode()
        ).hexdigest()
        pair[name] = {
            "first_request_normalized_sha256": body_hash,
            "target_after_call": received[0],
            "answer": response_text(run),
        }
    assert (
        pair["fixed-query"]["first_request_normalized_sha256"]
        == pair["fixed-auto"]["first_request_normalized_sha256"]
    )
    for case in ("fixed-sequential", "controlled"):
        run = runs[case]
        a, b, c = (first(run, phase) for phase in ("image-only", "last-clue", "stable"))
        assert [request["injected"] for request in (a, b, c)] == [0, 1, 0]
        assert all(not visible(run, request) for request in (a, b, c))
        assert a["retained_receipts"] == ["R2"] and not b["retained_receipts"]
        assert b["catalogs"][0]["view_id"] == c["catalogs"][0]["view_id"]
    controlled = runs["controlled"]
    group = first(controlled, "stable")["catalogs"][0]["text"]
    assert " · 2 张 · R3" in group and not re.search(r"\bR2\b", group)
    assert first(controlled, "directory-lost")["injected"] == 1
    assert first(runs["simultaneous"], "simultaneous")["injected"] == 1

    native = runs["native"]
    compaction = [
        {"seq": event["seq"], "type": event["type"], "data": event["data"]}
        for event in native["events"]
        if event["type"].startswith("compaction/")
    ]
    assert [event["type"] for event in compaction] == [
        "compaction/start",
        "compaction/summary",
        "compaction/end",
    ]
    after = first(native, "after-native-compact")
    assert (
        after["injected"] == 1 and not visible(native, after) and not after["receipts"]
    )
    blocks = after["body"]["messages"][0]["content"]
    index = next(
        i
        for i, block in enumerate(blocks)
        if block.get("text") == "</compacted-summary>"
    )
    assert blocks[index + 1]["text"] == after["catalogs"][0]["text"]

    offload = runs["offload"]
    offloads = [
        event for event in offload["events"] if event["type"] == "image/offload"
    ]
    assert len(offloads) == 1
    assert len(offloads[0]["data"]["targets"]) == 1
    retry = first(offload, "native-offload")
    assert retry["injected"] == 1 and not visible(offload, retry)
    failure = next(
        item["chunk"]["reason"]["failure"]
        for event in offload["events"]
        if event["type"] == "assistant/attempt"
        for item in event["data"]["stream"]
        if item.get("chunk", {}).get("type") == "finish"
    )
    assert failure["code"] == "IMAGE_OFFLOAD_REQUIRED"

    notes = runs["notes"]["notes"]
    assert [note["source"] for note in notes].count("user") == 2
    assert [note["source"] for note in notes].count("agent") == 1
    assert {note["target"] for note in notes} == {"R2"}
    assert len({note["event_id"] for note in notes}) == len(notes)
    assert [
        row["written"]
        for row in runs["notes"]["writes"]
        if row["kind"] == "user-feedback"
    ] == [True, True, True, False]
    return {
        "fixed_pair": pair,
        "compaction": [
            {
                "seq": event["seq"],
                "type": event["type"],
                **{
                    key: value
                    for key, value in event["data"].items()
                    if key in {"compactionId", "shadowedSeqs", "shadowedRange"}
                },
            }
            for event in compaction
        ],
        "compaction_catalog_after_text": blocks[index]["text"],
        "native_offload": {
            "event_seq": offloads[0]["seq"],
            **offloads[0]["data"],
            "failure": failure,
        },
        "persisted_notes": notes,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("runs", type=Path, nargs="+")
    args = parser.parse_args()
    paths = {
        case: next(path / case for path in args.runs if (path / case).is_dir())
        for case in CASES
    }
    runs = {case: read_run(path) for case, path in paths.items()}
    summary = verify(runs)
    summary["runs"] = {
        name: {
            "target_png_sha256": run["target"],
            "model_requests": [
                {
                    key: value
                    for key, value in row.items()
                    if key
                    in {
                        "n",
                        "phase",
                        "purpose",
                        "model",
                        "images",
                        "catalogs",
                        "receipts",
                        "injected",
                        "missing",
                        "compact_checkpoint",
                    }
                }
                | {"status": 200, "usage": run["usage"].get(row["n"])}
                for row in run["requests"]
            ],
            "tools": run["tools"],
            "writes": run["writes"],
        }
        for name, run in runs.items()
    }
    save_report(args.out, summary)


if __name__ == "__main__":
    main()
