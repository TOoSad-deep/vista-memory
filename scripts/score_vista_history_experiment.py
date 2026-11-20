"""Score image-history pairs from saved native DSH requests and tool events."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from dsh_sdk_run import save_report
from run_vista_history_experiment import BUDGET, conditions

READ = "vista_history_read"
CATALOG = {"vista_history_catalog", "vista_history_expand"}
TOOLS = CATALOG | {READ}
DIRECTIONS = {
    "上": r"朝上|向上|上方|顶部|十二点|12\s*点|top|upward",
    "下": r"朝下|向下|下方|底部|六点|6\s*点|bottom|downward",
    "左": r"朝左|向左|左[侧方]|九点|9\s*点|left",
    "右": r"朝右|向右|右[侧方]|三点|3\s*点|right",
}


def load(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def answer_score(answer: str, case: dict) -> str:
    expected = case["answer"]
    if expected is None:
        return "observation"
    if not answer:
        return "absent"
    if expected == "无":
        return (
            "correct"
            if re.search(
                r"没有|没保存|无.{0,5}(?:图|版本)|未存|未找到|未.{0,5}保存|无法确认",
                answer,
            )
            else "review"
        )
    uncertain = re.search(
        r"没(?:有|找到|看到)|无法|不确定|不能确认|未(?:找到|看到)", answer
    )
    if expected in DIRECTIONS:
        found = {
            key
            for key, pattern in DIRECTIONS.items()
            if re.search(pattern, answer, re.I)
        }
        bare = answer.strip("。！. \n")
        if found == {expected} and not uncertain or bare == expected:
            return "correct"
        return (
            "incorrect"
            if len(found) == 1 and expected not in found and not uncertain
            else "review"
        )
    found = set(re.findall(r"[蓝绿紫红黄黑白]色", answer))
    if found == {expected + "色"} and not uncertain:
        return "correct"
    return "incorrect" if len(found) == 1 and not uncertain else "review"


def canonical_first(request: dict, workspace: str) -> dict | None:
    body = request.get("body")
    if body is None:
        return None
    cards = {row["text"] for row in request.get("catalogs", [])}
    files = {
        row["fileId"]: row["sha256"]
        for row in request.get("images", [])
        if row.get("fileId") and row.get("sha256")
    }
    run_root = str(Path(workspace).parent) if workspace else None

    def scrub(obj):
        if isinstance(obj, dict):
            return {k: scrub(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [
                scrub(item)
                for item in obj
                if not (
                    isinstance(item, dict)
                    and item.get("type") == "text"
                    and item.get("text") in cards
                )
            ]
        if isinstance(obj, str):
            if run_root:
                obj = obj.replace(run_root, "{isolated-run-root}")
            return files.get(obj, obj)
        return obj

    normalized = scrub(body)
    if "messages" in normalized:
        normalized["messages"] = [
            m for m in normalized["messages"] if m.get("content") != []
        ]
    return normalized


def score(
    root: Path, materials: dict, case: dict, arm: str
) -> tuple[dict, dict | None]:
    report = load(root / "run.json", {})
    wire_path = root / "wire.jsonl"
    wire = (
        [json.loads(line) for line in wire_path.read_text().splitlines()]
        if wire_path.exists()
        else []
    )
    prepared = [row for row in wire if row.get("kind") == "request"]
    requests = [row for row in prepared if row["n"] <= BUDGET["maxModelRequests"]]
    budget_blocked = [row["n"] for row in prepared if row not in requests]
    statuses = {
        row["n"]: row["status"] for row in wire if row.get("kind") == "response"
    }
    valid = [row for row in requests if 200 <= statuses.get(row["n"], 0) < 300]
    target = case.get("target")
    archived = target or "R3"
    target_hash = materials["records"][archived]["sha256"]
    current_hash = materials["records"][materials["current"]]["sha256"]
    first_images = requests[0].get("images", []) if requests else []
    cards = requests[0].get("catalogs", []) if requests else []
    calls, errors, responses, answers = {}, {}, [], []
    for event in report.get("events", []):
        data = event.get("data", {})
        match event.get("type"):
            case "tool/call":
                args = data.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                calls[data["callId"]] = (data["name"], args)
            case "tool/result":
                message = data["message"]
                if message.get("isError"):
                    errors[message.get("toolCallId")] = "\n".join(
                        block["text"]
                        for block in message["content"]
                        if block.get("type") == "text"
                    )
            case "assistant/message":
                text = "\n".join(
                    block["text"]
                    for block in data["message"]["content"]
                    if block.get("type") == "text"
                )
                if text:
                    answers.append(text)
            case "turn/end":
                responses.append(data["reason"])
    read_calls = {
        call: args.get("short_id")
        for call, (name, args) in calls.items()
        if name == READ and args.get("content", "image") in {"image", "both"}
    }
    reads = [short for call, short in read_calls.items() if call not in errors]
    relevant = set(case.get("relevant", [target] if target else []))
    shown_target = [
        (row["n"], image["toolCallId"])
        for row in valid
        for image in row.get("images", [])
        if target is not None
        and image.get("sha256") == target_hash
        and read_calls.get(image.get("toolCallId")) == target
    ]
    seen = [row["n"] for row in valid if row.get("catalogs")]
    expected_card = materials["views"]["B2" if arm in {"auto", "query"} else arm]
    first = requests[0] if requests else {}
    first_body = json.dumps(first.get("body"), ensure_ascii=False)
    exact_cards = all(
        card["text"] == expected_card["text"]
        for req in valid
        for card in req["catalogs"]
    )
    delivery_ok = bool(
        first in valid
        and [item.get("sha256") for item in first_images] == [current_hash]
        and case["question"] in first_body
        and materials["checkpoint"] in first_body
        and set(first.get("tools", [])) == TOOLS
        and first.get("model") == "deepseek-flash"
        and exact_cards
        and (len(cards) == 0 if arm == "query" else len(cards) == 1)
    )
    reason = responses[-1] if responses else {}
    terminal = reason.get("kind")
    answer = answers[-1] if answers else ""
    actual_failures = [
        row["n"] for row in requests if row["n"] not in statuses or row not in valid
    ]
    per_request_usage = {
        row["n"]: row["usage"]
        for row in wire
        if row.get("kind") == "usage" and row.get("n") is not None
    }
    usage = Counter()
    for counters in per_request_usage.values():
        usage.update(
            {key: value for key, value in counters.items() if type(value) is int}
        )
    usage_complete = bool(valid) and all(row["n"] in per_request_usage for row in valid)
    input_tokens = None
    if (
        usage_complete
        and {"inputTokens", "cacheReadTokens", "cacheWriteTokens"} <= usage.keys()
    ):
        input_tokens = (
            usage["inputTokens"] + usage["cacheReadTokens"] + usage["cacheWriteTokens"]
        )
    stopping = None
    if case["id"].startswith("absent-") and terminal == "completed":
        if set(reads) >= (set(materials["records"]) - {materials["current"]}):
            stopping = "exhaustive"
        elif any("读图额度已用完" in error for error in errors.values()):
            stopping = "limit-rejected"
        elif len(reads) >= 3:
            stopping = "stopped-at-limit-without-rejection"
        elif actual_failures:
            stopping = "after-request-error"
        else:
            stopping = "voluntary"
    row = {
        "id": root.name,
        "arm": arm,
        "case": case["id"],
        "target": target,
        "delivery_ok": delivery_ok,
        "model_requests": len(requests),
        "request_audit_missing": not requests,
        "terminal": terminal,
        "turn_error": reason.get("error"),
        "first_layer": cards,
        "expected_view": expected_card["view_id"],
        "catalog_requests": seen,
        "catalog_calls": sum(name in CATALOG for name, _ in calls.values()),
        "opened": reads,
        "image_read_attempts": list(read_calls.values()),
        "initial_correct": list(read_calls.values())[:1] == [target]
        if target
        else None,
        "target_png_requests": shown_target,
        "unrelated_reads": sum(item not in relevant for item in reads),
        "extra_round_trips": max(0, len(valid) - 1) if requests else None,
        "answer": answer,
        "answer_grade": answer_score(answer, case),
        "stopping": stopping,
        "tool_failures": errors,
        "request_failures": actual_failures,
        "model_budget_blocked": budget_blocked,
        "error": report.get("error"),
        "input_tokens": input_tokens,
        "total_tokens": usage.get("totalTokens") if usage_complete else None,
        "cache_read_tokens": usage.get("cacheReadTokens") if usage_complete else None,
        "cache_write_tokens": usage.get("cacheWriteTokens") if usage_complete else None,
        "usage_fields": dict(usage),
    }
    return row, canonical_first(
        requests[0], report.get("workspace", "")
    ) if requests else None


def summarize(out: Path) -> dict:
    manifest = load(out / "frozen.json", {})
    rows, first = {}, {}
    pairs = []
    for identifier, materials, case, arm in conditions(manifest):
        rows[identifier], first[identifier] = score(
            out / "runs" / identifier, materials, case, arm
        )
    for materials in manifest["sets"]:
        for case in materials["cases"]:
            compare = (
                [("query", "auto")]
                if case["id"] == "delivery"
                else [("A", "B1"), ("B1", "B2")]
            )
            for x, y in compare:
                ids = [f"{materials['task']}-{case['id']}-{arm}" for arm in (x, y)]
                left, right = ids
                same = first[left] is not None and first[left] == first[right]
                pairs.append(
                    {
                        "cases": ids,
                        "first_request_equivalent_except_card": same,
                        "delivery_ok": same
                        and all(rows[i]["delivery_ok"] for i in ids),
                    }
                )
    return {"cases": list(rows.values()), "pairs": pairs}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = summarize(args.root)
    save_report(args.output or args.root / "scores.json", result)
    delivered = sum(row["delivery_ok"] for row in result["pairs"])
    print(f"Comparable deliveries: {delivered}/{len(result['pairs'])} pairs")


if __name__ == "__main__":
    main()
