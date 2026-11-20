"""Prepare and score the two T05 follow-ups against the archived 006 materials."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from dsh_sdk_run import save_report
from PIL import Image
from score_shader_recall_outcomes import code_equivalent, json_lines, load, source
from shader_recall_score import call_arguments, epoch, normalized_events

PROJECT = Path(__file__).resolve().parents[1]
ARCHIVE = PROJECT / "docs/test-results/20261117-png-shader-recall"
BASE = ARCHIVE / "diagnostic-v1/runs/006"
OUT = ARCHIVE / "focused-followup"
LIST = "mcp__vista__list_records"
READ = "mcp__vista__read_record"
INSTRUCTION = (
    "修改前，请先通过 VISTA 列出记录，"
    "从目录中找到当前选中版本紧前一次正式渲染的旧版本。"
    "取回它的渲染图和完整 Shader 源码，"
    "与当前版本比较铜环的窄高光和环宽，再决定怎样继续。"
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def original_config() -> dict:
    return next(
        entry["config"]
        for row in load(BASE / "overlay.json")
        for entry in row.get("insert", [])
        if entry["id"] == "recall-business"
    )


def checkpoint() -> dict:
    return load(Path(original_config()["checkpointPath"]))


def notes(fixture: dict) -> dict:
    rows = {}
    previous = None
    geometry = None
    for step in fixture["steps"]:
        mapping = fixture["mapping"][step["candidateId"]]
        png = Path(mapping["imagePath"])
        src = Path(mapping["sourcePath"])
        if (
            sha(png.read_bytes()) != step["imageHash"]
            or sha(src.read_bytes()) != step["sourceHash"]
        ):
            raise ValueError("Frozen candidate material changed")
        dimensions = [
            line.strip()
            for line in source(src).splitlines()
            if line.strip().startswith(
                ("vec2 center=", "vec2 extent=", "float r=length(p)")
            )
        ]
        if len(dimensions) != 3:
            raise ValueError("Cannot verify ellipse geometry")
        shape = (dimensions, step["uniforms"]["u_width"])
        if geometry and shape != geometry:
            raise ValueError("The stated geometry differs across frozen candidates")
        geometry = shape
        with Image.open(png) as im:
            hist = im.convert("L").histogram()
        peak = max(i for i, count in enumerate(hist) if count)
        count = sum(hist[80:])
        if previous is None:
            note = (
                f"本版确定了椭圆中心、长短轴和环宽。整幅渲染的最高灰度为 {peak}"
                f"（0–255），灰度达到 80 的像素有 {count} 个。"
            )
        else:
            direction = "增加" if count > previous else "减少"
            note = (
                f"椭圆中心、长短轴和环宽沿用前版。整幅渲染的最高灰度为 {peak}，"
                f"灰度达到 80 的像素由 {previous} 个{direction}至 {count} 个。"
            )
        previous = count
        rows[step["candidateId"]] = dict(
            text=note,
            peak=peak,
            pixels_ge_80=count,
            source_sha256=step["sourceHash"],
            image_sha256=step["imageHash"],
        )
    return rows


def enriched_history(original: str, descriptions: dict) -> str:
    result = original
    for cid, row in descriptions.items():
        pattern = rf"(### Attempt \d+: {re.escape(cid)}\n\[host fact\][^\n]*)"
        result, count = re.subn(
            pattern, lambda m: m[0] + "\n[版本说明] " + row["text"], result
        )
        if count != 1:
            raise ValueError(f"Expected one exact history section for {cid}")
    return result


def baseline_user_prompt() -> str:
    events = normalized_events(load(BASE / "probe.json")["events"])
    return next(
        e["data"]["content"][0]["text"]
        for e in events
        if e["type"] == "user/message"
        and e["data"].get("source", {}).get("kind") == "user"
    )


def prepare_turn(stage, output, config, restored, prompt, images):
    """Execute after restore and model resolution but before session/prompt."""
    from run_shader_recall import file_manifest

    original = original_config()
    expected_config = {
        k: original[k]
        for k in (
            "businessRoot",
            "checkpointPath",
            "referencePath",
            "requirements",
            "accessView",
            "providedImageNames",
            "currentCandidateId",
            "maxRenderAttempts",
            "maxListCalls",
            "maxReadCalls",
            "imageControl",
            "imageCondition",
            "maxModelRequests",
            "timeoutSeconds",
        )
    }
    old = load(BASE / "session-restore-check.json")
    initial = load(BASE / "prepared-prompt.json")
    ws = Path(restored["workspace"])
    old_ws = Path(old["workspace"])
    fixture = checkpoint()
    normalized_prompt = prompt.replace(str(output), str(BASE))
    baseline_files = file_manifest(old_ws)
    baseline_history = (old_ws / "HISTORY.md").read_text()
    checks = {
        "model_capabilities": load(output / "model-capabilities.json")
        == load(BASE / "model-capabilities.json"),
        "configuration": all(config.get(k) == v for k, v in expected_config.items()),
        "original_user_text_including_budget": normalized_prompt
        == baseline_user_prompt(),
        "tool_schemas": restored["tools"] == old["tools"],
        "workspace_before_treatment": file_manifest(ws) == baseline_files,
        "catalog_and_contents": file_manifest(Path(restored["vistaRoot"]))
        == load(BASE / "initial-record-manifest.json")
        == fixture["vistaManifest"],
        "initial_attachments": [
            (i["name"], sha(Path(i["path"]).read_bytes())) for i in images
        ]
        == [(i["name"], sha(Path(i["path"]).read_bytes())) for i in initial["images"]],
    }
    if not all(checks.values()):
        save_report(output / "preflight.json", checks)
        raise ValueError(
            "Focused input differs from 006 before treatment: "
            + str([k for k, v in checks.items() if not v])
        )
    if stage == "011":
        prompt += "\n" + INSTRUCTION
    else:
        text = enriched_history(baseline_history, notes(fixture))
        path = ws / "HISTORY.md"
        path.write_text(text)
        task_path = Path(restored["directory"]) / "task.json"
        task = load(task_path)
        if task["history"]["path"] != str(path):
            raise ValueError("Unexpected task history pointer")
        task["history"]["hash"] = sha(text.encode())
        save_report(task_path, task)
    (output / "initial-HISTORY.md").write_bytes((ws / "HISTORY.md").read_bytes())
    save_report(
        output / "preflight.json",
        {
            "baseline": str(BASE),
            "checks": checks,
            "stage": stage,
            "history_sha256": sha((ws / "HISTORY.md").read_bytes()),
            "normalized_user_prompt_sha256": sha(
                prompt.replace(str(output), str(BASE)).encode()
            ),
        },
    )
    return prompt


def check_launch(args):
    stage = args.focused_run
    original = original_config()
    if not (
        args.command == "checkpoint"
        and args.output.resolve() == OUT / "runs" / stage
        and args.checkpoint.resolve() == Path(original["checkpointPath"])
        and (args.provider, args.model) == ("deepseek-account", "deepseek-flash")
        and args.condition == "natural"
        and args.access_view == "discovery"
        and not any(
            (
                args.image_access_check,
                args.provided_records,
                args.generation_task,
                args.free_task,
                args.mcp_modes,
            )
        )
    ):
        raise ValueError("Focused follow-up configuration differs from predeclared run")
    if stage == "012" and not grade("011")["pass"]:
        raise ValueError("012 requires the 011 list/read/wire pre-edit chain")


def command(stage: str) -> list[str]:
    c = original_config()
    return [
        sys.executable,
        str(PROJECT / "scripts/run_shader_recall.py"),
        "checkpoint",
        "--focused-run",
        stage,
        "--checkpoint",
        c["checkpointPath"],
        "--output",
        str(OUT / "runs" / stage),
        "--provider",
        "deepseek-account",
        "--model",
        "deepseek-flash",
        "--condition",
        "natural",
        "--access-view",
        c["accessView"],
        "--image-condition",
        c["imageCondition"],
        "--requirements",
        c["requirements"],
        "--max-model-requests",
        str(c["maxModelRequests"]),
        "--max-render-attempts",
        str(c["maxRenderAttempts"]),
        "--max-list-calls",
        str(c["maxListCalls"]),
        "--max-read-calls",
        str(c["maxReadCalls"]),
        "--timeout",
        str(c["timeoutSeconds"]),
    ]


def first_edit(run, events, fixture):
    baseline = source(
        Path(fixture["mapping"][fixture["currentCandidateId"]]["sourcePath"])
    )
    snapshots = run / "workspace-source-snapshots"
    audits = {
        r["callId"]: r["changes"]
        for r in json_lines(run / "workspace-shader-edits.jsonl")
    }
    mutators = {
        "bash",
        "write",
        "edit",
        "apply_patch",
        "select_shader_candidate",
        "experiment_shader",
        "render_shader",
    }
    unknown = []
    for e in events:
        data = e["data"]
        if e["type"] != "tool/call" or data["name"] not in mutators:
            continue
        ref = {
            "call_id": data["callId"],
            "tool": data["name"],
            "seq": e["seq"],
            "time": e["time"],
        }
        if data["callId"] not in audits:
            unknown.append({**ref, "reason": "missing workspace shader snapshot"})
            continue
        for change in audits[data["callId"]]:
            # Creating an identical copy of the baseline and touching formatting alone
            # do not change its effective shader. Deletions are conservative cutoffs.
            before = (
                source(snapshots / change["before"]) if change["before"] else baseline
            )
            after = source(snapshots / change["after"]) if change["after"] else ""
            if not code_equivalent(before, after):
                return {**ref, **change}, unknown
        args = call_arguments(e) or {}
        if data["name"] in {"render_shader", "experiment_shader"} and args.get(
            "filePath"
        ):
            file = Path(args["filePath"])
            if file.is_absolute() and not file.is_relative_to(run / "workspace"):
                unknown.append(
                    {**ref, "reason": "render from outside tracked workspace"}
                )
    return None, unknown


def tool_evidence(events, fixture):
    steps = {s["record"]: s for s in fixture["steps"]}
    calls = {}
    lists, reads = [], []
    for e in events:
        data = e["data"]
        if e["type"] == "tool/call" and data["name"] in (LIST, READ):
            args = call_arguments(e) or {}
            row = dict(
                call_id=data["callId"],
                call_seq=e["seq"],
                call_step=data["step"],
                call_time=e["time"],
                success=False,
                result_seq=None,
                result_time=None,
            )
            calls[data["callId"]] = row
            if data["name"] == LIST:
                row.update(records=[])
                lists.append(row)
            else:
                row.update(
                    record=args.get("record"),
                    mode=args.get("content", "image"),
                    png=False,
                    glsl=False,
                    parameters=False,
                )
                reads.append(row)
        if e["type"] != "tool/result":
            continue
        message = data["message"]
        row = calls.get(message["toolCallId"])
        if row is None:
            continue
        row.update(
            result_time=e["time"],
            result_seq=e["seq"],
            is_error=bool(message.get("isError")),
        )
        if row["is_error"]:
            continue
        text = "\n".join(b["text"] for b in message["content"] if b["type"] == "text")
        try:
            obj, _ = json.JSONDecoder().raw_decode(text.lstrip())
        except ValueError:
            continue
        if "records" in row:
            if isinstance(obj, dict) and isinstance(obj.get("records"), list):
                row.update(
                    success=True,
                    records=[
                        r["record"]
                        for r in obj["records"]
                        if isinstance(r, dict) and "record" in r
                    ],
                )
        elif (
            isinstance(obj, dict)
            and obj.get("record") == row["record"]
            and row["record"] in steps
        ):
            step = steps[row["record"]]
            code = source(Path(fixture["mapping"][step["candidateId"]]["sourcePath"]))
            row["png"] = any(
                b.get("attachment", {}).get("attachmentId")
                == "sha256:" + step["imageHash"]
                and b["attachment"].get("mediaType") == "image/png"
                for b in message["content"]
            )
            row["glsl"] = code in text
            row["parameters"] = (
                obj.get("render", {}).get("uniforms") == step["uniforms"]
            )
            modes = {
                "image": row["png"],
                "code": row["glsl"],
                "both": row["png"] and row["glsl"],
            }
            row["success"] = modes.get(row["mode"], False)
    return lists, reads


def wire_chain(step, lists, reads, requests, received, edit, unknown, metadata_hash):
    target = step["record"]
    catalog = [item for item in lists if item["success"] and target in item["records"]]
    successful = [r for r in reads if r["success"] and r["record"] == target]
    ordered = [
        r
        for r in successful
        if any(
            listing["result_seq"] < r["call_seq"]
            and listing["call_step"] < r["call_step"]
            for listing in catalog
        )
    ]
    visible = []
    winners = []
    for req in requests:
        rid = req["requestId"]
        imgs = [
            im
            for im in req.get("images", [])
            if im.get("sha256") == step["imageHash"]
            and im.get("mimeType") == "image/png"
            and not im.get("unresolvedFile")
        ]
        code = step["sourceHash"] in req.get("includedCodeSha256", [])
        if not (imgs or code):
            continue
        code_rows = [
            c for c in req.get("codeSources", []) if c["sha256"] == step["sourceHash"]
        ]
        prior = {
            r["call_id"]: r
            for r in ordered
            if epoch(r["result_time"]) < epoch(req["time"])
        }
        image_calls = [
            i["toolCallId"]
            for i in imgs
            if i.get("toolCallId") in prior
            and prior[i["toolCallId"]]["png"]
            and i.get("source", {}).get("channel") == READ
            and i["source"].get("record") == target
        ]
        code_calls = [
            c["toolCallId"]
            for c in code_rows
            if c.get("toolCallId") in prior and prior[c["toolCallId"]]["glsl"]
        ]
        before = (
            edit is not None
            and epoch(req["time"]) < epoch(edit["time"])
            and all(epoch(req["time"]) < epoch(u["time"]) for u in unknown)
        )
        status = received.get(rid)
        ok = bool(
            image_calls
            and code
            and code_calls
            and before
            and status is not None
            and 200 <= status < 300
        )
        visible.append(
            dict(
                request=rid,
                time=req["time"],
                http_status=status,
                png=bool(imgs),
                glsl=code,
                parameters=metadata_hash in req.get("includedMetadataSha256", []),
                image_origins=[
                    dict(
                        call_id=i.get("toolCallId"),
                        source=i.get("source"),
                        location=i["location"],
                    )
                    for i in imgs
                ],
                code_origins=code_rows,
                after_ordered_read=bool(prior),
                before_edit_or_ambiguous_action=before,
                proven_read_png=image_calls,
                proven_read_glsl=code_calls,
            )
        )
        if ok:
            winners.append(rid)
    return {
        "record": target,
        "catalog_result_seqs": [listing["result_seq"] for listing in catalog],
        "successful_read_result_seqs": [r["result_seq"] for r in successful],
        "ordered_read_result_seqs": [r["result_seq"] for r in ordered],
        "wire_visibility": visible,
        "qualifying_requests": winners,
    }


def grade(stage):
    run = OUT / "runs" / stage
    probe = load(run / "probe.json")
    events = normalized_events(probe.get("events", []))
    http = json_lines(run / "http-audit.jsonl")
    responses = {r["requestId"]: r["status"] for r in http if r["type"] == "response"}
    requests = [
        r
        for r in http
        if r["type"] == "request"
        and r.get("model") == "deepseek-flash"
        and r.get("sessionId") == probe["session_id"]
    ]
    fixture = checkpoint()
    lists, reads = tool_evidence(events, fixture)
    edit, unknown = first_edit(run, events, fixture)
    steps = fixture["steps"][:-1]
    target = steps[-1]
    chains = []
    for s in steps:
        rec = load(Path(fixture["vistaRoot"]) / "json" / s["record"])
        metadata = dict(
            record=s["record"],
            label=rec["label"],
            source_agent=rec["source_agent"],
            render=rec["render"],
        )
        h = sha(json.dumps(metadata, ensure_ascii=False).encode())
        chains.append(
            wire_chain(s, lists, reads, requests, responses, edit, unknown, h)
        )
    relevant = [chains[-1]] if stage == "011" else chains
    gates = {
        "directory_list_returned_relevant_record": any(
            c["catalog_result_seqs"] for c in relevant
        ),
        "successful_read_after_directory_on_a_later_model_step": any(
            c["ordered_read_result_seqs"] for c in relevant
        ),
        "same_accepted_model_request_with_read_png_and_full_glsl_before_edit": any(
            c["qualifying_requests"] for c in relevant
        ),
    }
    checks = load(run / "preflight.json") if (run / "preflight.json").exists() else {}
    initial_ok = bool(checks.get("checks")) and all(checks["checks"].values())
    control_errors = [
        r
        for r in http
        if r["type"] == "image-control-error"
        and r.get("message") != "Model request budget exceeded"
    ]
    valid = (
        initial_ok
        and probe.get("template_unchanged") is True
        and not control_errors
        and not (run / "image-control-failure.json").exists()
    )
    task_path = run / "business/restore-probe/task.json"
    task = load(task_path) if task_path.exists() else {}
    current_round = task.get("workflow", {}).get("rounds", [{}])[-1]
    return {
        "stage": stage,
        "pass": bool(all(gates.values()) and valid),
        "gates": gates,
        "first_failed_gate": next((k for k, v in gates.items() if not v), None),
        "evidence_valid": bool(valid),
        "first_substantive_edit": edit,
        "edit_status": "observed"
        if edit
        else "incomplete-audit"
        if unknown
        else "no-observed-workspace-shader-edit",
        "unverified_actions": unknown,
        "list_calls": lists,
        "read_calls": reads,
        "record_chains": chains,
        "immediate_predecessor": target["record"],
        "immediate_predecessor_chain_pass": bool(chains[-1]["qualifying_requests"]),
        "run_status": probe["status"],
        "turn_reason": probe.get("turn_reason"),
        "run_error": probe.get("error"),
        "model_requests": len(requests),
        "new_candidate_ids": current_round.get("candidateIds", []),
        "submitted_candidate": current_round.get("submittedId"),
        "control_errors": control_errors,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["prepare", "run", "grade"])
    ap.add_argument("stage", choices=["011", "012"], nargs="?")
    args = ap.parse_args()
    if args.action == "prepare":
        f = checkpoint()
        original = (
            Path(load(BASE / "session-restore-check.json")["workspace"]) / "HISTORY.md"
        ).read_text()
        descriptions = notes(f)
        text = enriched_history(original, descriptions)
        if re.sub(r"^\[版本说明\][^\n]*\n", "", text, flags=re.M) != original:
            raise ValueError("History changed beyond descriptions")
        dest = OUT / "preparation"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "HISTORY-012.md").write_text(text)
        save_report(
            dest / "conditions.json",
            {
                "baseline": str(BASE),
                "checkpoint": original_config()["checkpointPath"],
                "model": load(BASE / "model-capabilities.json"),
                "explicit_instruction_011": INSTRUCTION,
                "normalized_original_user_text_sha256": sha(
                    baseline_user_prompt().encode()
                ),
                "evidence_for_012_descriptions": descriptions,
                "history_006_sha256": sha(original.encode()),
                "history_012_sha256": sha(text.encode()),
                "commands": {s: command(s) for s in ("011", "012")},
                "runtime_variation": [
                    "isolated paths and identifiers for sessions, calls and uploads",
                    "wall time, remaining seconds, model sampling and service state",
                    "registration repair and host-only shader and HTTP audit",
                ],
            },
        )
        print(dest)
        return 0
    if not args.stage:
        ap.error("run and grade require a stage")
    if args.action == "run":
        return subprocess.run(command(args.stage), cwd=PROJECT).returncode
    result = grade(args.stage)
    save_report(OUT / f"score-{args.stage}.json", result)
    print(
        json.dumps(
            {
                k: result[k]
                for k in ("stage", "pass", "gates", "first_failed_gate", "edit_status")
            }
        )
    )
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
