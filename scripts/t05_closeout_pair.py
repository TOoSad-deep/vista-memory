"""Fork the frozen T05 post-render snapshot for a two-request DS closeout."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from dsh_sdk_run import DEFAULT_CLI, RpcProcess, save_report
from run_shader_recall import BUSINESS, PROJECT, build_overlay
from shader_trigger_validation import json_lines, load, version

BASE = PROJECT / "docs/test-results/20261119-t05-local-restore/regression"
OUT = PROJECT / "docs/test-results/20261119-t05-post-render-pair"
WAKE = "SHADER_CHECKPOINT_RESUME_INTERNAL"
REV = "42b6773e482df0140baca53a1f9d9e147705e0f3"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def prepare(active, sid, treatment):
    active.mkdir()
    state = load(BASE / "session-restore-check.json")
    old_id = state["sessionId"]
    base_task = BASE / "business/restore-probe"
    task = load(base_task / "task.json")
    rnd = task["workflow"]["rounds"][-1]
    (candidate,) = rnd["candidateIds"]
    assert not rnd.get("submittedId") and not task["candidates"][-1].get("assessment")
    manifest = base_task / "candidates" / candidate / "comparison.json"
    old_hash = sha(manifest.read_bytes())
    new_hash = sha(
        manifest.read_text()
        .replace(str(BASE), str(active))
        .replace(old_id, sid)
        .encode()
    )

    def rebind(text):
        return (
            text.replace(str(BASE), str(active))
            .replace(old_id, sid)
            .replace(old_hash, new_hash)
        )

    for sub in ("workspace", "business/restore-probe", "vista", "home/attachments"):
        shutil.copytree(
            BASE / sub,
            active / sub,
            ignore=shutil.ignore_patterns(".recall-audit", "diag"),
        )
    bind = active / "business/.conversations" / sid / "session.json"
    bind.parent.mkdir(parents=True)
    bind.write_text(
        (BASE / "business/.conversations" / old_id / "session.json").read_text()
    )
    shutil.copy2(
        BASE / "business/vista-records.json", active / "business/vista-records.json"
    )
    for sub in ("workspace", "business"):
        for p in (active / sub).rglob("*"):
            if p.is_file() and p.suffix in {".json", ".jsonl", ".md"}:
                p.write_text(rebind(p.read_text()))
    assert (
        sha(
            (
                active
                / "business/restore-probe/candidates"
                / candidate
                / "comparison.json"
            ).read_bytes()
        )
        == new_hash
    )
    assert (
        sha(
            (
                active / "business/restore-probe/candidates" / candidate / "render.png"
            ).read_bytes()
        )
        == task["candidates"][-1]["imageHash"]
    )
    rows = json_lines(next((BASE / "home/sessions").rglob("session.v4.jsonl")))
    rows = [
        json.loads(rebind(json.dumps(r, ensure_ascii=False)))
        for r in rows
        if "seq" not in r or r["seq"] <= 81
    ]
    assert rows[-1]["type"] == "step/end" and rows[-1]["data"]["step"] == 10
    renderrow = next(r for r in rows if r.get("seq") == 80)
    message = renderrow["data"]["message"]
    text = message["content"][0]
    payload = json.loads(text["text"])
    recall = next(r for r in rows if r.get("seq") == 48)["data"]["message"]
    recalled = json.loads(recall["content"][0]["text"])
    # Reuse the production comparator on the two actual archived publications.
    js = """import { RecalledCandidates } from './src/candidate-recall.js';
      import { readFileSync } from 'node:fs';
      const v=JSON.parse(readFileSync(0,'utf8'));let step=1;
      const turnBoundaryOf=()=>({openTurnStartSeq:1,lastStepStartSeq:step});
      const a=new RecalledCandidates({turnBoundaryOf});
      const session={}; a.remember(session,v.task,v.recalled,v.recallId);step=2;
      process.stdout.write(JSON.stringify(a.compare(session,v.task,v.result).recallComparison));"""
    comparison = json.loads(
        subprocess.check_output(
            ["node", "--input-type=module", "-e", js],
            cwd=BUSINESS,
            input=json.dumps(
                dict(
                    task=task["id"],
                    recalled=recalled,
                    recallId=recall["toolCallId"],
                    result=payload,
                )
            ).encode(),
        )
    )
    assert payload["id"] == candidate and comparison[0]["exactMatch"]
    if treatment:
        payload["recallComparison"] = comparison
        text["text"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    w = active / "workspace"
    slug = "--" + re.sub(r"[^a-zA-Z0-9_.-]", "-", str(w)).lstrip("-")[:251] + "--"
    log = active / "home/sessions" / slug / sid / "session.v4.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    # Fresh provider state binds each uploaded file to its bytes.
    hashes = sorted(
        {
            x.name
            for x in (active / "home/attachments/v1/objects").rglob("*")
            if x.is_file()
        }
    )
    at_render = renderrow["time"] / 1000
    start = next(r for r in rows if r.get("type") == "turn/start")["time"] / 1000
    budget_seconds = int(600 - (at_render - start))
    return dict(
        renderCall=message["toolCallId"],
        renderDigest=sha(text["text"].encode()),
        candidate=candidate,
        expectedReceipt=comparison if treatment else None,
        sessionId=sid,
        manifestHash=new_hash,
        images=hashes,
        wake=WAKE,
    ), budget_seconds


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["preview", "live"])
    ap.add_argument("arm", choices=["control", "receipt"])
    ap.add_argument("work", type=Path)
    a = ap.parse_args()
    preview = a.mode == "preview"
    version(BUSINESS, REV)
    work = a.work.resolve()
    current = work / "run"
    out = (work if preview else OUT) / (a.mode + "-" + a.arm)
    out.mkdir(parents=True, exist_ok=False)
    sid = str(uuid4())
    guard, seconds = prepare(current, sid, a.arm == "receipt")
    official = load(BASE / "session-restore-check.json")
    checkpoint = load(BASE / "preflight.json")
    ref = official["referenceRecord"]["record"]
    cfg = dict(
        output=str(current),
        workspace=str(current / "workspace"),
        businessRoot=str(BUSINESS),
        projectRoot=str(PROJECT),
        python=sys.executable,
        referencePath=str(current / "business/restore-probe/reference.png"),
        vistaRoot=str(current / "vista"),
        taskId="restore-probe",
        sessionId=sid,
        renderedCheckpoint={"referenceRecord": ref},
        nativeAuthor=True,
        maxRenderAttempts=2,
        maxModelRequests=2,
        timeoutSeconds=seconds,
        maxListCalls=2,
        maxReadCalls=8,
    )
    overlay = build_overlay(cfg, current)
    entries = load(overlay)
    wire = out / "wire.jsonl"
    ctrl = work / "preview-control/preview.json"
    if not preview:
        p1 = load(ctrl)
        p2 = load(work / "preview-receipt/preview.json")
        assert (
            p1["signature"] == p2["signature"]
            and p1["receipt"] is None
            and p2["receipt"][0]["exactMatch"]
        )
    sig = load(ctrl)["signature"] if ctrl.exists() else None
    # Keep the first normalized body under the local temporary directory for inspection.
    entries.append(
        {
            "insert": [
                {
                    "id": "post-render-wire-check",
                    "name": str(PROJECT / "scripts/t05_closeout_wire.mjs"),
                    "config": {
                        **guard,
                        "log": str(wire),
                        "preview": preview,
                        "previewBody": str(work / ("body-" + a.arm + ".json")),
                        "expectedSignature": sig,
                    },
                }
            ]
        }
    )
    save_report(overlay, entries)
    report = dict(
        session=sid,
        source_cutoff_seq=81,
        model_request_budget=2,
        render_budget=2,
        seconds=seconds,
    )
    rpc = RpcProcess(
        [DEFAULT_CLI, "--profile", "sdk-minimal", "--patch", str(overlay)],
        current / "home",
        current / "workspace",
    )
    try:
        rpc.request(
            "initialize",
            dict(
                cwd=str(current / "workspace"),
                provider="deepseek-account",
                model="deepseek-flash",
                reasoningEffort="high",
                maxTokens=8192,
            ),
            time.monotonic() + 90,
        )
        rpc.request("session/resume", {"sessionId": sid}, time.monotonic() + 100)
        ready = load(current / "session-restore-check.json")
        assert (
            ready["tools"] == official["tools"]
            and load(current / "model-capabilities.json") == checkpoint["model"]
        )
        rpc.request(
            "session/prompt",
            {"sessionId": sid, "contentBlocks": [{"type": "text", "text": WAKE}]},
            time.monotonic() + 45,
        )
        rpc.request("session/wait", {"sessionId": sid}, time.monotonic() + seconds)
    except Exception as ex:
        report["error"] = str(ex)
    finally:
        try:
            rpc.request("shutdown", {}, time.monotonic() + 15)
            rpc.process.wait(timeout=12)
            rpc.close_streams()
        except Exception:
            rpc.close()
        report["events"] = [
            m["params"]["event"]
            for m in rpc.notifications
            if m.get("method") == "session.event"
        ]
        report["stderr"] = rpc.stderr
        save_report(out / "run.json", report)
        shutil.move(str(current), str(out / "snapshot"))
    rows = json_lines(wire)
    if preview:
        first = next((r for r in rows if r["type"] == "preview"), None)
        if first:
            save_report(out / "preview.json", first)
        if not first:
            raise RuntimeError("Preview did not reach the model boundary")
    else:
        if not any(r["type"] == "response" and r["status"] == 200 for r in rows):
            raise RuntimeError("No accepted DS response")
    print(
        json.dumps(
            {
                "arm": a.arm,
                "mode": a.mode,
                "requests": sum(r["type"] == "request" for r in rows),
                "preview": sum(r["type"] == "preview" for r in rows),
                "error": report.get("error"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
