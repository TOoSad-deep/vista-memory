"""Check recorded DSH acceptance evidence without making model requests."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

TOOLS = {"mcp__vista__list_records", "mcp__vista__read_record"}


def load(path: Path):
    return json.loads(path.read_text())


def seconds(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def check(result: dict, name: str, passed: bool) -> None:
    result["checks"].append({"name": name, "passed": bool(passed)})


def finish(result: dict) -> dict:
    result["status"] = (
        "passed" if all(item["passed"] for item in result["checks"]) else "failed"
    )
    return result


def read_audit(path: Path) -> tuple[list[dict], list[str]]:
    if not path.exists():
        return [], ["HTTP audit file is missing"]
    requests = []
    pending = {}
    problems = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        try:
            event = json.loads(line)
        except ValueError:
            problems.append(f"Invalid HTTP audit line {number}")
            continue
        kind = event.get("type")
        request_id = event.get("requestId")
        if kind == "request":
            if request_id in pending:
                problems.append(f"Request ID reused before response at line {number}")
            requests.append(event)
            pending[request_id] = event
        elif kind in {"response", "transport-error"}:
            request = pending.pop(request_id, None)
            if request is None:
                problems.append(f"Unmatched response at line {number}")
            else:
                request["response"] = event
    if pending:
        problems.append("HTTP requests still lack a response")
    return requests, problems


def case_requests(case: dict, requests: list[dict]) -> list[dict]:
    start = seconds(case["started_at"])
    end = seconds(case["finished_at"])
    return [
        request
        for request in requests
        if request.get("sessionId") == case["session_id"]
        and start <= seconds(request["time"]) <= end
    ]


def inspect_case(case: dict, records: dict, requests: list[dict]) -> dict:
    result = {
        "id": case["id"],
        "session_id": case["session_id"],
        "checks": [],
        "answers": [],
        "usage": {},
        "reads": [],
        "catalogs": [],
        "tool_sequence": [],
    }
    if case.get("status") == "running":
        result["status"] = "pending"
        return result
    check(result, "Agent turn completed", case.get("status") == "completed")
    if not case.get("finished_at"):
        return finish(result)
    outgoing = case_requests(case, requests)
    check(result, "Actual HTTP requests captured", bool(outgoing))
    result["http_request_count"] = len(outgoing)
    events = [
        item["params"]["event"]
        for item in case.get("events", [])
        if item.get("method") == "session.event"
        and item.get("params", {}).get("sessionId") == case["session_id"]
    ]
    calls = {}
    usage = Counter()
    header_count = 0
    for event in events:
        data = event.get("data", {})
        kind = event["type"]
        if kind == "request/header":
            header_count += 1
            check(
                result,
                f"Canonical tool set at event {event['seq']}",
                {tool["name"] for tool in data["header"]["tools"]} == TOOLS,
            )
        elif kind == "assistant/message":
            usage.update(data.get("usage", {}))
            content = data.get("message", {}).get("content", [])
            texts = [block["text"] for block in content if block["type"] == "text"]
            if texts:
                result["answers"].append("\n".join(texts))
        elif kind == "tool/call":
            check(result, f"Allowed tool {data['name']}", data["name"] in TOOLS)
            check(result, "Unique tool call ID", data["callId"] not in calls)
            calls[data["callId"]] = data
            result["tool_sequence"].append(data["name"])
        elif kind == "tool/result":
            message = data["message"]
            call = calls.get(message["toolCallId"])
            check(result, "Tool result has matching call", call is not None)
            if call is None:
                continue
            call["result_seen"] = True
            arguments = json.loads(call["arguments"])
            content = message["content"]
            if call["name"] == "mcp__vista__list_records":
                check(result, "List call succeeded", not message["isError"])
                if message["isError"]:
                    continue
                check(result, "Catalog is a single text block", len(content) == 1)
                catalog = json.loads(content[0]["text"])["records"]
                result["catalogs"].append(catalog)
                check(
                    result,
                    "Catalog summaries match registered fixtures",
                    len({item["record"] for item in catalog}) == len(catalog)
                    and all(
                        item == records.get(item["record"], {}).get("summary")
                        for item in catalog
                    ),
                )
                continue
            record_name = arguments.get("record")
            mode = arguments.get("content", "image")
            read = {
                "record": record_name,
                "mode": mode,
                "content_omitted": "content" not in arguments,
                "is_error": message["isError"],
                "event_seq": event["seq"],
            }
            result["reads"].append(read)
            if message["isError"]:
                check(result, "Expected error scenario", case["id"].startswith("D7-"))
                read["error_text"] = "\n".join(
                    block.get("text", "") for block in content
                )
                check(result, "Error includes an explanation", bool(read["error_text"]))
                continue
            expected = records.get(record_name)
            check(result, "Read identifies a known fixture", expected is not None)
            if expected is None:
                continue
            types = ["text"]
            if mode in {"image", "both"}:
                types.append("image")
            check(
                result,
                f"{record_name} {mode} content block types",
                [b["type"] for b in content] == types,
            )
            texts = [block["text"] for block in content if block["type"] == "text"]
            # DSH coalesces adjacent MCP text blocks with a newline.
            combined = "\n".join(texts)
            metadata, metadata_end = json.JSONDecoder().raw_decode(combined)
            check(
                result,
                f"{record_name} metadata",
                metadata == expected["metadata"],
            )
            later_requests = [
                request
                for request in outgoing
                if seconds(request["time"]) >= event["time"] / 1000
            ]
            if mode in {"code", "both"}:
                code = expected["code"]
                check(
                    result,
                    f"{record_name} complete code",
                    combined[metadata_end:]
                    == "\n" + (code if code is not None else "没有关联代码"),
                )
                if code is not None:
                    check(
                        result,
                        f"{record_name} code in later HTTP request",
                        any(
                            expected["code_sha256"]
                            in request.get("includedCodeSha256", [])
                            for request in later_requests
                        ),
                    )
            if mode in {"image", "both"}:
                images = [
                    block.get("attachment", {})
                    for block in content
                    if block["type"] == "image"
                ]
                check(
                    result,
                    f"{record_name} attachment hash",
                    len(images) == 1
                    and images[0].get("attachmentId")
                    == "sha256:" + expected["png_sha256"],
                )
                check(
                    result,
                    f"{record_name} attachment MIME",
                    len(images) == 1 and images[0].get("mediaType") == "image/png",
                )
                check(
                    result,
                    f"{record_name} image in later HTTP request",
                    any(
                        image["sha256"] == expected["png_sha256"]
                        for request in later_requests
                        for image in request.get("images", [])
                    ),
                )
            if mode == "image":
                check(
                    result,
                    f"{record_name} image-only text",
                    combined[metadata_end:] == "",
                )
    result["usage"] = dict(usage)
    check(
        result,
        "Every tool call has a result",
        bool(calls) and all(call.get("result_seen") for call in calls.values()),
    )
    # A continued turn can reuse a previously logged request header.
    if case["id"] != "D5-append":
        check(result, "Canonical request header captured", header_count > 0)
    return finish(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory", type=Path, default=Path("docs/test-results/20261116-dsh")
    )
    args = parser.parse_args()
    directory = args.directory
    fixture = load(directory / "fixtures.json")
    labels = fixture["records"]
    records = {
        item["summary"]["record"]: item for item in labels.values() if "summary" in item
    }
    specifications = {
        case["id"]: case
        for file in sorted(directory.glob("cases-*.json"))
        for case in load(file)
    }
    cases = {}
    runs = {}
    run_for_case = {}
    duplicate_ids = []
    for file in sorted(directory.glob("run-*.json")):
        run = load(file)
        runs[file.name] = run
        for case in run.get("cases", []):
            if case["id"] in cases:
                duplicate_ids.append(case["id"])
            cases[case["id"]] = case
            run_for_case[case["id"]] = file.name
            if case.get("append_summary"):
                summary = case["append_summary"]
                original = labels["r-b"]
                records[summary["record"]] = {
                    **labels["append"],
                    "summary": summary,
                    "metadata": {
                        key: summary[key] for key in ["record", "label", "source_agent"]
                    }
                    | {"render": labels["append"]["render"]},
                    "code": original["code"],
                    "code_sha256": original["code_sha256"],
                }
    requests, audit_problems = read_audit(directory / "http-audit.jsonl")
    assessments = {}
    for case_id in specifications:
        if case_id not in cases:
            assessments[case_id] = {
                "id": case_id,
                "status": "pending",
                "reason": "Case has not run",
                "checks": [],
            }
            continue
        try:
            assessments[case_id] = inspect_case(cases[case_id], records, requests)
        except (ValueError, KeyError, TypeError) as error:
            assessments[case_id] = {
                "id": case_id,
                "status": "failed",
                "reason": f"Invalid evidence: {error}",
                "checks": [],
            }
        assessments[case_id]["run_file"] = run_for_case[case_id]

    ref, b, c, d = (
        labels[name]["summary"]["record"] for name in ["r-a", "r-b", "r-c", "r-d"]
    )
    expected_reads = {
        "D1": [(ref, "image", False)],
        "D2-image": [(b, "image", False)],
        "D2-code": [(b, "code", False)],
        "D2-both": [(b, "both", False)],
        "D3": [(ref, "code", False), (ref, "both", False)],
        "D4": [(b, "both", False), (c, "both", False), (d, "both", False)],
        "D6": [(ref, "image", False)],
        "D7-missing-record": [
            ("record-does-not-exist.json", "image", True),
            (ref, "image", False),
        ],
        "D7-corrupt-record": [
            ("record-corrupt.json", "image", True),
            (ref, "image", False),
        ],
        "D7-missing-image": [(b, "image", True), (b, "code", False)],
    }
    appended = cases.get("D5-append", {}).get("append_summary", {}).get("record")
    if appended:
        expected_reads["D5-append"] = [(appended, "both", False)]
    for case_id, assessment in assessments.items():
        if assessment["status"] == "pending" or "reads" not in assessment:
            continue
        reads = assessment["reads"]
        actual = [(read["record"], read["mode"], read["is_error"]) for read in reads]
        check(
            assessment,
            "Requested scenario reads occurred",
            all(item in actual for item in expected_reads.get(case_id, [])),
        )
        if case_id == "D1":
            check(
                assessment,
                "Default content argument omitted",
                any(
                    read["content_omitted"] and read["record"] == ref for read in reads
                ),
            )
        if case_id in {"D1", "D6"}:
            check(
                assessment,
                "Catalog discovery precedes reading",
                assessment["tool_sequence"][:2]
                == ["mcp__vista__list_records", "mcp__vista__read_record"],
            )
        if case_id == "D2-code":
            check(
                assessment,
                "Code-only session has no HTTP images",
                all(
                    not request.get("images")
                    for request in case_requests(cases[case_id], requests)
                ),
            )
        if case_id in {"D1", "D5-before", "D5-append", "D6"}:
            expected_count = 4 if case_id in {"D1", "D5-before"} else 5
            check(
                assessment,
                "Expected catalog size",
                bool(assessment["catalogs"])
                and all(
                    len(catalog) == expected_count for catalog in assessment["catalogs"]
                ),
            )
        if case_id == "D5-append":
            check(
                assessment,
                "Append summary used directly",
                bool(reads)
                and reads[0]["record"] == appended
                and bool(assessment["catalogs"])
                and any(
                    item["record"] == appended for item in assessment["catalogs"][-1]
                ),
            )
            check(
                assessment,
                "New record is read before catalog scanning",
                assessment["tool_sequence"][:2]
                == ["mcp__vista__read_record", "mcp__vista__list_records"],
            )
        if case_id.startswith("D7-"):
            check(
                assessment,
                "Expected error followed by successful recovery",
                len(reads) >= 2
                and reads[0]["is_error"]
                and any(
                    not read["is_error"] and read["event_seq"] > reads[0]["event_seq"]
                    for read in reads[1:]
                ),
            )
        finish(assessment)

    suite = {"id": "D8-and-suite", "checks": []}
    check(suite, "Unique case results", not duplicate_ids)
    source_hashes = {}
    for label, expected in labels.items():
        filename = "r-e.png" if label == "append" else label + ".png"
        source = directory / filename
        digest = (
            hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else None
        )
        source_hashes[filename] = digest
        check(
            suite, f"Archived source PNG {filename}", digest == expected["png_sha256"]
        )
    check(
        suite,
        "Shared and changed code fixture versions",
        labels["r-b"]["code"] == labels["r-c"]["code"]
        and labels["r-b"]["code"] != labels["r-d"]["code"],
    )
    d2_ids = ["D2-image", "D2-code", "D2-both"]
    check(
        suite,
        "Three modes use separate sessions",
        len({specifications[key]["session_id"] for key in d2_ids}) == 3,
    )
    if all(key in cases for key in ["D5-before", "D5-append"]):
        check(
            suite,
            "Live append reuses the running session and process",
            cases["D5-before"]["session_id"] == cases["D5-append"]["session_id"]
            and run_for_case["D5-before"] == run_for_case["D5-append"],
        )
    if "D6" in cases and cases["D6"].get("status") != "running":
        restart = runs[run_for_case["D6"]]
        check(
            suite,
            "New process initialized for D6",
            run_for_case["D6"] != run_for_case.get("D1")
            and "initialize_response" in restart
            and restart.get("process_exit_code") == 0,
        )
        check(
            suite,
            "New session used for D6",
            cases["D6"]["session_id"]
            not in {case["session_id"] for key, case in cases.items() if key != "D6"},
        )
        check(
            suite,
            "D6 discovers existing records",
            bool(assessments["D6"].get("catalogs")),
        )
    check(
        suite, "HTTP audit records are complete", bool(requests) and not audit_problems
    )
    known_hashes = {item["png_sha256"] for item in labels.values()}
    for number, request in enumerate(requests, 1):
        check(
            suite,
            f"HTTP request {number} tools",
            set(request.get("tools", [])) == TOOLS,
        )
        check(
            suite,
            f"HTTP request {number} response",
            request.get("response", {}).get("status") == 200,
        )
        check(
            suite,
            f"HTTP request {number} known images",
            all(
                image.get("sha256") in known_hashes
                and image.get("mimeType") == "image/png"
                for image in request.get("images", [])
            ),
        )
    finish(suite)
    usage = Counter()
    for item in assessments.values():
        usage.update(item.get("usage", {}))
    statuses = Counter(item["status"] for item in assessments.values())
    status = "passed"
    if "failed" in statuses or suite["status"] == "failed":
        status = "failed"
    elif "pending" in statuses:
        status = "pending"
    report = {
        "status": status,
        "case_counts": dict(statuses),
        "cases": assessments,
        "suite": suite,
        "audit_problems": audit_problems,
        "source_png_sha256": source_hashes,
        "usage": dict(usage),
        "http_request_count": len(requests),
        "http_image_occurrences": sum(
            len(request.get("images", [])) for request in requests
        ),
        "requested_reasoning_efforts": sorted(
            {
                run.get("initialize_params", {}).get("reasoningEffort", "unknown")
                for run in runs.values()
            }
        ),
        "wire_top_level_reasoning_efforts": sorted(
            {request.get("reasoningEffort", "unknown") for request in requests}
        ),
        "wire_configuration_reasoning_efforts": sorted(
            {
                effort
                for request in requests
                for effort in request.get("configurationReasoningEfforts", [])
            }
        ),
        "visual_answer_review": "Visual answers require separate review.",
    }
    (directory / "assessment.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                key: report[key]
                for key in [
                    "status",
                    "case_counts",
                    "usage",
                    "http_request_count",
                    "http_image_occurrences",
                ]
            },
            ensure_ascii=False,
        )
    )
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
