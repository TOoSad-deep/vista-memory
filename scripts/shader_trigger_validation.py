"""Run and inspect the T05 business feedback validation for revision 7fc72bb."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from dsh_sdk_run import save_report
from score_shader_recall_outcomes import json_lines, load
from shader_recall_followup import (
    BASE,
    PROJECT,
    READ,
    checkpoint,
    first_edit,
    original_config,
    tool_evidence,
)
from shader_recall_score import call_arguments, epoch, normalized_events

OUT = PROJECT / "docs/test-results/20261118-business-trigger-7fc72bb"
VISTA_OUT = PROJECT / "docs/test-results/20261118-vista-first-t05"
ACCEPTANCE_OUT = PROJECT / "docs/test-results/20261118-t05-recall-acceptance"
REV = "7fc72bbfce14e097954cc18691cedc322b3a6850"
NATIVE_REV = "cceb8315473245a2ce371df9401118ec05c5aef4"
NATIVE_OUT = PROJECT / "docs/test-results/20261119-t05-native-author"
FEEDBACK = {
    "regression": "这版铜环的高光比上一版弱了，恢复之前的窄高光，保持环的宽度。",
    "parameter": "请将当前 Shader 的 u_shine 默认值改为 0.95，"
    "保持其他参数和程序逻辑不变。",
}
FEEDBACK["vista-first"] = FEEDBACK["regression"]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def version(host, revision=REV):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(host), *args])

    head = git("rev-parse", "HEAD").decode().strip()
    if head != revision:
        raise ValueError(f"Unexpected business revision {head}")
    paths = ["src", "skills", "runtime", "package.json"]
    git("diff", "--exit-code", revision, "--", *paths)
    if git("ls-files", "--others", "--exclude-standard", "--", *paths):
        raise ValueError("Untracked runtime source")
    script = (
        "import {createHash} from 'node:crypto';"
        "import {ShaderService} from './src/service.js';"
        "import {visualRecallPolicy} from './src/prompts/visual-recall.js';"
        "console.log(JSON.stringify([createHash('sha256').update("
        "ShaderService.prototype.continue.toString()).digest('hex'),visualRecallPolicy]));"
    )
    method, policy = json.loads(
        subprocess.check_output(["node", "--input-type=module", "-e", script], cwd=host)
    )
    return dict(
        head=head, root=str(host), paths=paths, method_sha256=method, policy=policy
    )


def verify_before_turn(output, config, restored, fixture, images):
    original = original_config()
    host = Path(restored["businessRoot"]).resolve()
    native = bool(config.get("nativeAuthor"))
    runtime = version(host, config.get("nativeRevision", NATIVE_REV) if native else REV)
    loaded = load(output / "loaded-author.json")
    caps = load(output / "model-capabilities.json")
    step = fixture["steps"][-2]
    target = fixture["mapping"][step["candidateId"]]
    code = Path(target["sourcePath"]).read_bytes()
    original_images = load(BASE / "prepared-prompt.json")["images"]
    same_settings = [
        "businessRoot",
        "referencePath",
        "requirements",
        "checkpointPath",
        "imageCondition",
        "currentCandidateId",
        "accessView",
        "maxRenderAttempts",
        "maxReadCalls",
        "maxListCalls",
        "maxModelRequests",
        "timeoutSeconds",
        "providedImageNames",
    ]
    checks = {
        "loaded_business_module": loaded["businessRoot"]
        == str(host)
        == original["businessRoot"]
        and runtime["method_sha256"] == loaded["continuationMethodSha256"]
        and runtime["policy"] == loaded["policy"],
        "model": caps == load(BASE / "model-capabilities.json")
        and (caps["provider"], caps["id"], caps["name"])
        == ("deepseek-account", "deepseek-flash", "DeepSeek-V4.1-Flash"),
        "checkpoint_and_budget": all(original[k] == config[k] for k in same_settings),
        "copied_records": load(output / "initial-record-manifest.json")
        == fixture["vistaManifest"]
        == load(BASE / "initial-record-manifest.json"),
        "target": step["step"] == 5
        and step["record"] == fixture["mapping"][step["candidateId"]]["record"]
        and digest(code) == step["sourceHash"]
        and digest(Path(target["imagePath"]).read_bytes()) == step["imageHash"],
        "initial_images": [
            (v["name"], digest(Path(v["path"]).read_bytes())) for v in images
        ]
        == [(v["name"], digest(Path(v["path"]).read_bytes())) for v in original_images],
        "sole_feedback": loaded["feedback"]
        == config["businessFeedback"]
        == FEEDBACK[output.name],
        "original_history": (Path(restored["workspace"]) / "HISTORY.md").read_bytes()
        == (
            Path(load(BASE / "session-restore-check.json")["workspace"]) / "HISTORY.md"
        ).read_bytes(),
    }
    pair = output.parent == ACCEPTANCE_OUT
    if native:
        cue = loaded["hostSuffix"]
        checks.update(
            formal_tool=any(
                tool["name"] == "recall_shader_candidate" for tool in restored["tools"]
            ),
            native_feedback="experimentalHostCue" not in config
            and "nativeHostSuffix" not in loaded,
            natural_cue=(
                "recall_shader_candidate" in cue
                if output.name == "regression"
                else not cue
            ),
            no_target_in_feedback_context=all(
                identifier
                not in cue
                + loaded["feedback"]
                + load(output / "prepared-prompt.json")["content"][0]["text"]
                for identifier in (target["record"], step["candidateId"])
            ),
        )
    if output.name == "vista-first" or pair:
        baseline = OUT / ("parameter" if output.name == "parameter" else "regression")
        prev = load(baseline / "session-restore-check.json")
        old = load(baseline / "loaded-author.json")
        cue = (
            (VISTA_OUT / "host-cue.txt").read_text().strip()
            if output.name != "parameter"
            else ""
        )
        cue = cue.replace("{selected}", fixture["currentCandidateId"]).replace(
            "{directory}", restored["directory"]
        )

        def normalized(text):
            return text.replace(str(output), str(baseline))

        snapshots = (
            load(output / "prepared-prompt.json"),
            load(baseline / "prepared-prompt.json"),
        )
        checks.update(
            same_tool_schemas=restored["tools"] == prev["tools"],
            same_checkpoint_prompt=normalized(snapshots[0]["content"][0]["text"])
            == snapshots[1]["content"][0]["text"],
            same_business_wrapper=normalized(
                load(output / "business-continuation.json")["content"][0]["text"]
            )
            == load(baseline / "business-continuation.json")["content"][0]["text"],
            only_experimental_suffix=normalized(
                loaded.get("nativeHostSuffix", loaded["hostSuffix"])
            )
            == old["hostSuffix"]
            and loaded["hostSuffix"] == cue
            and loaded["policy"] == old["policy"],
            no_old_identifiers_in_cue=all(
                s[key] not in cue
                for s in fixture["steps"][:-1]
                for key in ("candidateId", "record")
            ),
        )
    save_report(
        output / "preflight.json",
        dict(runtime=runtime, pid=loaded["pid"], model=caps, checks=checks),
    )
    if not all(checks.values()):
        raise ValueError(f"Preflight failure: {checks}")
    save_report(
        output / "wire-expectations.json",
        dict(
            feedback=loaded["feedback"],
            cue=loaded["hostSuffix"],
            policy=runtime["policy"],
            code=code.decode(),
            glsl=step["sourceHash"],
            png=step["imageHash"],
            render=target["render"],
            **({"fingerprintToolText": True} if pair or native else {}),
            record=step["record"],
            candidate=step["candidateId"],
        ),
    )


def verified_candidate_parameters(message, wire_digest, target, render):
    """Decode candidate JSON inside mixed shell output only on exact wire bytes."""
    for block in message["content"]:
        text = block.get("text")
        if not text or digest(text.encode()) != wire_digest:
            continue
        for match in re.finditer(r"(?m)^[ \t]*\{", text):
            try:
                candidate, _ = json.JSONDecoder().raw_decode(
                    text[match.start() :].lstrip()
                )
            except ValueError:
                continue
            if (
                candidate.get("id") == target["candidateId"]
                and candidate.get("sourceHash") == target["sourceHash"]
                and candidate.get("imageHash") == target["imageHash"]
                and all(
                    candidate.get(k) == render[k] for k in ("width", "height", "time")
                )
                and {p["name"]: p["default"] for p in candidate.get("params", [])}
                == render["uniforms"]
            ):
                return True
    return False


def evaluate(stage, dest=None):
    run = (dest or (VISTA_OUT if stage == "vista-first" else OUT)) / stage
    fixture = checkpoint()
    target = fixture["steps"][-2]
    probe = load(run / "probe.json")
    events = normalized_events(probe["events"])
    edit, unknown = first_edit(run, events, fixture)
    listings, reads = tool_evidence(events, fixture)
    successful = {
        e["data"]["message"]["toolCallId"]: e
        for e in events
        if e["type"] == "tool/result" and not e["data"]["message"].get("isError")
    }
    calls = {e["data"]["callId"]: e for e in events if e["type"] == "tool/call"}
    user_text = [
        [b["text"] for b in e["data"]["content"] if b["type"] == "text"]
        for e in events
        if e["type"] == "user/message"
        and e["data"].get("source", {}).get("kind") == "user"
    ]
    http = json_lines(run / "http-audit.jsonl")
    status = {r["requestId"]: r["status"] for r in http if r["type"] == "response"}
    requests = [
        r
        for r in http
        if r.get("type") == "request"
        and r.get("model") == "deepseek-flash"
        and r.get("sessionId") == probe["session_id"]
    ]
    if not requests:
        raise ValueError("No DS model requests")
    initial = requests[0]

    def markers(row):
        return {k for v in row["triggerEvidence"] for k in v["kinds"]}

    cue = load(run / "loaded-author.json")["hostSuffix"]
    preflight = load(run / "preflight.json")
    native = preflight["checks"].get("formal_tool") is True
    host_cue = (
        ("recall_shader_candidate" in cue)
        if native
        else cue.startswith("Before editing, resolve the earlier candidate")
    ) and "host-cue" in markers(initial)
    valid = (
        all(preflight["checks"].values())
        and probe.get("template_unchanged")
        and user_text == [[FEEDBACK[stage]]]
        and "literal-feedback" in markers(initial)
        and 200 <= status.get(initial["requestId"], 0) < 300
        and "author-policy" in markers(initial)
        and (
            not native
            or (
                "recall_shader_candidate" in initial["tools"]
                and not any("test-budget" in markers(req) for req in requests)
            )
        )
        and not (run / "image-control-failure.json").exists()
        and not any(
            x.get("type") == "image-control-error"
            and x.get("message") != "Model request budget exceeded"
            for x in http
        )
    )
    historic = {s["candidateId"]: s["record"] for s in fixture["steps"][:-1]}
    material = []
    old_images = set()
    for req in requests:
        tm = epoch(req["time"])

        def confirmed(cid):
            return (
                cid in successful
                and cid in calls
                and calls[cid]["data"]["name"]
                in {READ, "read_image", "read", "bash", "recall_shader_candidate"}
                and epoch(successful[cid]["time"]) <= tm
            )

        images = []
        codes = []
        params = []
        parameter_blocks = {}
        for im in req["images"]:
            src = im.get("source", {})
            cid = im.get("toolCallId")
            rec = src.get("record") or historic.get(src.get("candidateId"))
            if (
                rec in historic.values()
                and confirmed(cid)
                and not im.get("unresolvedFile")
            ):
                old_images.add(rec)
                if (
                    rec == target["record"]
                    and im.get("sha256") == target["imageHash"]
                    and im.get("mimeType") == "image/png"
                ):
                    images.append(cid)
        for v in req["triggerEvidence"]:
            cid = v.get("callId")
            if confirmed(cid):
                if "full-target-glsl" in v["kinds"]:
                    codes.append(cid)
                matched_block = verified_candidate_parameters(
                    successful[cid]["data"]["message"],
                    v["textSha256"],
                    target,
                    fixture["mapping"][target["candidateId"]]["render"],
                )
                if "target-render-parameters" in v["kinds"] or matched_block:
                    params.append(cid)
                    parameter_blocks[cid] = v["textSha256"]
        if images or codes or params:
            material.append(
                dict(
                    request=req["requestId"],
                    status=status.get(req["requestId"]),
                    time=req["time"],
                    before=bool(
                        edit
                        and tm < epoch(edit["time"])
                        and all(tm < epoch(a["time"]) for a in unknown)
                    ),
                    png=images,
                    glsl=codes,
                    parameters=params,
                    parameter_block_sha256=parameter_blocks,
                )
            )
    qualifying = [
        r
        for r in material
        if r["before"]
        and 200 <= (r["status"] or 0) < 300
        and r["png"]
        and r["glsl"]
        and r["parameters"]
    ]
    active_target = (
        any(r["success"] and r["record"] == target["record"] for r in reads)
        or target["record"] in old_images
    )
    gates = dict(
        host_triggered=host_cue,
        selected_step_5=active_target,
        three_materials_before_edit=bool(qualifying),
    )
    receipts = json_lines(run / "business/restore-probe/candidate-recalls.jsonl")
    if native and stage == "regression":
        target_calls = {
            r["callId"]
            for r in receipts
            if r["candidateId"] == target["candidateId"]
            and r.get("record") == target["record"]
            and r["via"] == "vista"
            and r["status"] == "retrieved"
            and r["callId"] in successful
            and calls[r["callId"]]["data"]["name"] == "recall_shader_candidate"
            and (call_arguments(calls[r["callId"]]) or {}).get("candidateId")
            == target["candidateId"]
        }
        gates["formal_tool_same_call_three_materials"] = any(
            target_calls.intersection(r["png"], r["glsl"], r["parameters"])
            for r in qualifying
        )
    vista_requests = []
    inspect_vista_path = stage == "vista-first" or (
        dest == ACCEPTANCE_OUT and stage == "regression"
    )
    if inspect_vista_path:
        target_reads = {
            r["call_id"]
            for r in reads
            if r["record"] == target["record"]
            and r["success"]
            and any(
                listing["success"]
                and target["record"] in listing["records"]
                and listing["result_seq"] < r["call_seq"]
                and listing["call_step"] < r["call_step"]
                for listing in listings
            )
        }
        vista_requests = [
            r["request"]
            for r in qualifying
            if all(
                target_reads.intersection(r[kind])
                for kind in ("png", "glsl", "parameters")
            )
        ]
        if stage == "vista-first":
            gates["catalog_to_correct_vista_record_and_three_materials"] = bool(
                vista_requests
            )
    task = load(run / "business/restore-probe/task.json")
    rnd = task["workflow"]["rounds"][-1]
    relevant_tools = []
    prior_file_calls = []
    for cid, e in calls.items():
        name = e["data"]["name"]
        args = call_arguments(e) or {}
        if name in {
            READ,
            "read_image",
            "read",
            "bash",
            "mcp__vista__list_records",
            "recall_shader_candidate",
        }:
            path_or_command = args.get("file_path") or args.get("command", "")
            if any(key in path_or_command for key in historic):
                prior_file_calls.append(e["seq"])
            relevant_tools.append(
                dict(
                    seq=e["seq"],
                    tool=name,
                    success=cid in successful,
                    path=args.get("file_path") or args.get("record"),
                    call_id=cid,
                )
            )
    control = None
    if stage == "parameter":
        source = (run / "seed.frag").read_bytes()
        line = next(
            s for s in source.splitlines() if s.startswith(b"// param: u_shine ")
        )
        selected = rnd.get("submittedId")
        delivered = (
            (
                run / "business/restore-probe/candidates" / selected / "shader.frag"
            ).read_bytes()
            if selected
            else b""
        )
        old_programs = {s["sourceHash"] for s in fixture["steps"][:-1]} - {
            fixture["steps"][-1]["sourceHash"]
        }
        control = {
            "no_targeted_host_cue": not cue and "host-cue" not in markers(initial),
            "no_vista_lookup": not (listings or reads),
            "no_direct_historical_path_calls": not prior_file_calls,
            "no_old_images_or_complete_historical_programs_on_wire": not old_images
            and not any(
                old_programs.intersection(r["includedCodeSha256"]) for r in requests
            ),
            "submitted_only_u_shine_default_change": b"default: 0.2 " in line
            and delivered
            == source.replace(line, line.replace(b"default: 0.2 ", b"default: 0.95 ")),
        }
        if native:
            control["no_candidate_recall"] = not receipts and not any(
                c["data"]["name"] == "recall_shader_candidate" for c in calls.values()
            )
    return dict(
        stage=stage,
        feedback=FEEDBACK[stage],
        valid=bool(valid),
        gates=gates,
        control_checks=control,
        pass_stage=bool(
            valid and all((control if control is not None else gates).values())
        ),
        pass_regression=bool(stage == "regression" and valid and all(gates.values())),
        host_suffix=cue,
        raw_user_text=user_text,
        initial_request=initial["requestId"],
        first_wire_markers=sorted(markers(initial)),
        first_edit=edit,
        unknown_actions=unknown,
        target=target,
        lists=listings,
        reads=reads,
        tools=relevant_tools,
        historical_path_calls=prior_file_calls,
        historical_images=sorted(old_images),
        material=material,
        **(
            {
                "business_recall_receipts": receipts,
                "model_budget_exhausted": probe.get("model_budget_exhausted"),
                "author_submitted": bool(rnd.get("submittedId")),
                "formal_candidate_count": len(rnd["candidateIds"]),
                "informal_experiment_count": len(task.get("experimentLog", [])),
            }
            if native
            else {}
        ),
        qualifying_requests=[r["request"] for r in qualifying],
        **({"vista_qualifying_requests": vista_requests} if inspect_vista_path else {}),
        model_requests=len(requests),
        turn=probe.get("turn_reason"),
        submitted=rnd.get("submittedId"),
        new_candidates=rnd["candidateIds"],
        shutdown=probe.get("status"),
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("operation", choices=["run", "grade"])
    p.add_argument("stage", choices=list(FEEDBACK))
    p.add_argument(
        "--native-author",
        action="store_true",
        help="Run the two-case cceb831 validation",
    )
    p.add_argument(
        "--acceptance",
        action="store_true",
        help="Use the fresh T05 two-case acceptance suite",
    )
    args = p.parse_args()
    stage = args.stage
    if args.native_author and (args.acceptance or stage == "vista-first"):
        p.error("Select either native author or the legacy acceptance experiment")
    dest = (
        NATIVE_OUT
        if args.native_author
        else ACCEPTANCE_OUT
        if args.acceptance
        else VISTA_OUT
        if stage == "vista-first"
        else OUT
    )
    if args.acceptance and stage == "vista-first":
        p.error("Acceptance cases are regression and parameter")
    if args.operation == "grade":
        result = evaluate(stage, dest)
        save_report(dest / f"{stage}-audit.json", result)
        print(
            json.dumps(
                {
                    k: result[k]
                    for k in (
                        "stage",
                        "valid",
                        "pass_stage",
                        "control_checks",
                        "gates",
                        "first_edit",
                        "qualifying_requests",
                        "model_requests",
                        "submitted",
                    )
                },
                ensure_ascii=False,
            )
        )
        return
    if args.native_author:
        receipt = load(
            PROJECT
            / "docs/test-results/20261118-shader-author-candidate-recall"
            / "acceptance.json"
        )
        if receipt["business_commit"] != NATIVE_REV:
            raise SystemExit("Business entry regression is missing for this revision")
    elif args.acceptance:
        receipt = load(dest / "offline.json")
        if receipt.get("passed") is not True or receipt.get("revision") != REV:
            raise SystemExit(
                "T05 acceptance requires the offline normal and fallback checks"
            )
    elif stage == "parameter" and not evaluate("regression")["pass_regression"]:
        raise SystemExit("Control requires three verified regression gates")
    c = original_config()
    version(Path(c["businessRoot"]), NATIVE_REV if args.native_author else REV)
    command = [
        sys.executable,
        str(PROJECT / "scripts/run_shader_recall.py"),
        "checkpoint",
        "--output",
        str(dest / stage),
        "--checkpoint",
        c["checkpointPath"],
        "--provider",
        "deepseek-account",
        "--model",
        "deepseek-flash",
        "--condition",
        "natural",
        "--access-view",
        "discovery",
        "--requirements",
        c["requirements"],
        "--business-feedback",
        FEEDBACK[stage],
        "--max-render-attempts",
        str(c["maxRenderAttempts"]),
        "--max-model-requests",
        str(c["maxModelRequests"]),
        "--timeout",
        str(c["timeoutSeconds"]),
    ]
    if stage == "vista-first" or (args.acceptance and stage == "regression"):
        command += ["--experimental-host-cue", str(VISTA_OUT / "host-cue.txt")]
    if args.native_author:
        command += ["--native-author"]
    raise SystemExit(subprocess.run(command, cwd=PROJECT).returncode)


if __name__ == "__main__":
    main()
