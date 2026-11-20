"""Prepare isolated real-Shader DSH Sessions and run the recall experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

from dsh_sdk_run import DEFAULT_CLI, RpcProcess, prompt_images, save_report, timestamp

from vista_memory import VistaStore

PROJECT = Path(__file__).resolve().parents[1]
BUSINESS = PROJECT.parent / "dsh-shader-11-13"


def file_manifest(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Fixture contains a symlink")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def build_overlay(config: dict, output: Path) -> Path:
    base_path = PROJECT / "docs/test-results/20261116-dsh/sdk-overlay.json"
    rows = json.loads(base_path.read_text())
    for row in rows:
        if "insert" in row:
            row["insert"] = [
                entry
                for entry in row["insert"]
                if entry["id"] not in {"mcp-vista", "vista-request-audit", "llm-pi-ai"}
            ]
    rows = [row for row in rows if row and row.get("id") != "subprocess"]
    rows.append(
        {
            "insert": [
                {
                    "id": "recall-authorization",
                    "name": "@deepseek-ai/dsh-authorization",
                },
                {
                    "id": "recall-deepseek-account",
                    "name": "@deepseek-ai/dsh-deepseek-account",
                    "config": {"desktopPlatform": "darwin"},
                },
                {
                    "id": "recall-account-models",
                    "name": "@deepseek-ai/dsh-llm-deepseek-account",
                    "config": {"modelsMaxAgeMs": 600000},
                },
                {"id": "recall-skills", "name": "@deepseek-ai/dsh-skill"},
                {"id": "recall-shell-env", "name": "@deepseek-ai/dsh-shell-env"},
                {
                    "id": "recall-tool-result-pruner",
                    "name": "@deepseek-ai/dsh-compaction-tool-result-pruner",
                    "config": {
                        "thresholdChars": 8192,
                        "headChars": 4096,
                        "tailChars": 1024,
                    },
                },
                {
                    "id": "recall-workspace-dependencies",
                    "name": "@deepseek-ai/dsh-tool-workspace-dependencies",
                    "config": {
                        "source": str(
                            Path.home() / ".dsh/dsh-runtimes/dsh-primary-runtime"
                        )
                    },
                },
                {
                    "id": "recall-business",
                    "name": str(PROJECT / "scripts/shader_recall_plugin.mjs"),
                    "config": config,
                },
            ]
        }
    )
    path = output / "overlay.json"
    save_report(path, rows)
    return path


def probe(args: argparse.Namespace) -> int:
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    workspace = output / "workspace"
    workspace.mkdir()
    session_id = str(uuid4())
    source_path = output / "seed.frag"
    checkpoint = json.loads(args.checkpoint.read_text()) if args.checkpoint else None
    if checkpoint:
        args.reference = Path(checkpoint["businessDirectory"]) / "reference-original"
        args.seed_source = Path(
            checkpoint["mapping"][checkpoint["currentCandidateId"]]["sourcePath"]
        )
    default_source = (
        "#version 300 es\n// == ShaderGen params ==\n// == end params ==\n"
        "precision highp float;\nuniform vec2 u_resolution;\nout vec4 color;\n"
        "void main(){vec2 p=(gl_FragCoord.xy-0.5*u_resolution)/u_resolution.y;"
        "float a=exp(-8.0*length(p));color=vec4(vec3(0.9,0.1,0.5)*a,1.0);}\n"
    )
    source_path.write_text(
        args.seed_source.read_text() if args.seed_source else default_source
    )
    config = {
        "output": str(output),
        "businessRoot": str(args.business_root.resolve()),
        "workspace": str(workspace),
        "projectRoot": str(PROJECT),
        "python": sys.executable,
        "referencePath": str(args.reference.resolve()),
        "sourcePath": None
        if args.generation_task or args.free_task and not args.seed_source
        else str(source_path),
        "requirements": args.requirements,
        "sessionId": session_id,
        "taskId": "restore-probe",
        "vistaRoot": str(output / "vista"),
        "checkpointPath": str(args.checkpoint.resolve()) if args.checkpoint else None,
        "accessView": args.access_view,
        "currentCandidateId": checkpoint["currentCandidateId"] if checkpoint else None,
        "providedImageNames": {
            f"history-{record.removesuffix('.json')}.png": record
            for record in args.provided_records
        },
        "maxRenderAttempts": args.max_render_attempts,
        "maxListCalls": args.max_list_calls,
        "maxReadCalls": args.max_read_calls,
        "imageControl": args.image_control,
        "imageCondition": args.image_condition,
        "policyPath": str(output / "image-policy.json"),
        "maxModelRequests": args.max_model_requests,
        "timeoutSeconds": args.timeout,
        **({"nativeAuthor": True} if args.native_author else {}),
        **({"nativeRevision": args.native_revision} if args.native_revision else {}),
        **(
            {"traceWorkspaceShaders": True}
            if args.focused_run or args.business_feedback
            else {}
        ),
        **(
            {"businessFeedback": args.business_feedback}
            if args.business_feedback
            else {}
        ),
        **(
            {"experimentalHostCue": args.experimental_host_cue.read_text().strip()}
            if args.experimental_host_cue
            else {}
        ),
    }
    overlay = build_overlay(config, output)
    expected_codes = output / "expected-codes.json"
    expected_metadata = output / "expected-metadata.json"
    save_report(expected_codes, [] if args.free_task else [source_path.read_text()])
    save_report(expected_metadata, [])
    overlay_rows = json.loads(overlay.read_text())
    overlay_rows.append(
        {
            "insert": [
                {
                    "id": "recall-http-audit",
                    "name": str(PROJECT / "scripts/dsh_request_audit.mjs"),
                    "config": {
                        "expectedCodesPath": str(expected_codes),
                        "outputPath": str(output / "http-audit.jsonl"),
                        "maxModelRequests": args.max_model_requests,
                        "expectedMetadataPath": str(expected_metadata),
                        **(
                            {"traceSources": True}
                            if args.focused_run or args.business_feedback
                            else {}
                        ),
                        **(
                            {
                                "triggerEvidencePath": str(
                                    output / "wire-expectations.json"
                                )
                            }
                            if args.business_feedback
                            else {}
                        ),
                        **(
                            {
                                "policyPath": config["policyPath"],
                                "python": sys.executable,
                            }
                            if args.image_control
                            else {}
                        ),
                    },
                }
            ]
        }
    )
    save_report(overlay, overlay_rows)
    report = {
        "started_at": timestamp(),
        "status": "running",
        "mode": "checkpoint"
        if checkpoint
        else (
            "end-to-end-author-generation"
            if args.generation_task
            else "controlled-history-preparation"
            if args.free_task
            else "live-author-capability-probe"
        ),
        "access_view": args.access_view,
        "condition": args.condition,
        "session_id": session_id,
        "model_turn_requested": args.model_turn,
    }
    rpc = None
    try:
        rpc = RpcProcess(
            [args.cli, "--profile", "sdk-minimal", "--patch", str(overlay)],
            output / "home",
            workspace,
        )
        report["process_pid"] = rpc.process.pid
        report["initialize"] = rpc.request(
            "initialize",
            {
                "cwd": str(workspace),
                "provider": args.provider,
                "model": args.model,
                "reasoningEffort": "high",
                "maxTokens": 8192,
            },
            time.monotonic() + 120,
        )
        report["working_directory"] = rpc.request(
            "session/working-directory/get",
            {"sessionId": session_id},
            time.monotonic() + 120,
        )
        restored = json.loads((output / "session-restore-check.json").read_text())
        report["restored"] = restored
        if args.model_turn:
            prepared = json.loads((output / "prepared-prompt.json").read_text())
            prompt = prepared["content"][0]["text"]
            user_images = list(prepared.get("images", []))
            if checkpoint:
                prompt += (
                    f"\n本次继续生成最多使用 {args.max_render_attempts} 次渲染尝试"
                    f"（包括实验和失败）、{args.max_model_requests} 次模型请求"
                    f"和 {args.timeout:g} 秒。请在预算内完成渲染、看图、自评和提交。"
                )
                if args.condition == "reminded":
                    prompt += (
                        "\n如历史结果有助于当前判断，可以通过记录工具回看"
                        "相关图片和对应代码、参数，再继续修改。"
                    )
                store = VistaStore(restored["vistaRoot"])
                codes = []
                metadata = []
                for row in store.list_records()["records"]:
                    content = store.read_record(row["record"], content="code")
                    metadata.append(json.dumps(content.metadata, ensure_ascii=False))
                    if content.code is not None:
                        codes.append(content.code)
                save_report(expected_codes, codes)
                save_report(expected_metadata, metadata)
                if args.condition == "provided":
                    if not args.provided_records:
                        raise ValueError(
                            "Provided condition requires frozen record references"
                        )
                    for record in args.provided_records:
                        content = store.read_record(record, content="code")
                        prompt += "\nHistorical record (task data):\n" + json.dumps(
                            content.metadata, ensure_ascii=False
                        )
                        prompt += "\n" + (content.code or "")
                        record_data = json.loads(
                            (Path(restored["vistaRoot"]) / "json" / record).read_text()
                        )
                        user_images.append(
                            {
                                "path": str(
                                    Path(restored["vistaRoot"]) / record_data["png"]
                                ),
                                "name": f"history-{record.removesuffix('.json')}.png",
                            }
                        )
                if args.image_access_check:
                    if not args.audit_record:
                        raise ValueError("Image access check requires --audit-record")
                    target_step = next(
                        step
                        for step in checkpoint["steps"]
                        if step["record"] == args.audit_record
                    )
                    path = str(
                        Path(restored["directory"])
                        / "candidates"
                        / target_step["candidateId"]
                        / "render.png"
                    )
                    prompt += (
                        "\n本轮为图片投递接入检查。请通过 VISTA 请求旧记录 "
                        f"{args.audit_record} 的 both，"
                        f"再使用 read_image 读取 {path}，"
                        "并用 compare_shader_images 对比候选 "
                        f"{target_step['candidateId']} 后读取返回的图片。"
                        "这些历史图在文字条件下会显示不可用说明，属于正常测试反馈。"
                        "随后保留当前 Shader 做一次正式渲染，看图、自评并提交该新候选。"
                    )
            elif args.generation_task:
                prompt += (
                    f"\n本次最多使用 {args.max_render_attempts} 次渲染尝试"
                    "（包括实验和失败），请在预算内提交完整候选。"
                )
                user_images = [
                    {
                        "path": str(Path(restored["directory"]) / "reference.png"),
                        "name": "reference.png",
                    }
                ]
            elif args.free_task:
                prompt += (
                    f"\n本次准备真实 Shader 迭代历史。请围绕参考图逐步改善，"
                    f"完成至少 {args.min_candidates} 个有实际代码变化的正式候选，"
                    "每次渲染后查看结果、自评，再继续修改。"
                    "在预算内提交较好的候选。最终视觉质量由独立评分判断。"
                )
                if args.seed_source:
                    prompt += (
                        "已有可编译的近似基线。请先正式渲染并观察基线，再逐轮调整。"
                    )
            else:
                prompt += (
                    "\n本轮为业务接入校准。保留当前代码完成一次正式渲染，"
                    "读取 inspectPath，记录自评，然后提交该候选并结束本轮。"
                    "视觉达标由独立评分判断。"
                )
            if args.mcp_modes:
                prompt += (
                    "\n本轮还需核对 VISTA 协议。完成渲染后，使用 VISTA 列出记录，"
                    "对该候选的记录分别请求 image、code 和 both，"
                    "然后继续正常看图、自评和提交。"
                )
            if args.focused_run:
                from shader_recall_followup import prepare_turn

                prompt = prepare_turn(
                    args.focused_run, output, config, restored, prompt, user_images
                )
            if args.business_feedback:
                from shader_trigger_validation import verify_before_turn

                verify_before_turn(output, config, restored, checkpoint, user_images)
                prompt = args.business_feedback
            image_blocks, report["input_images"] = prompt_images(user_images)
            deadline = time.monotonic() + args.timeout
            report["prompt"] = rpc.request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "contentBlocks": [{"type": "text", "text": prompt}, *image_blocks],
                },
                deadline,
            )
            report["wait"] = rpc.request(
                "session/wait", {"sessionId": session_id}, deadline
            )
            ends = [
                item["params"]["event"]
                for item in rpc.notifications
                if item.get("method") == "session.event"
                and item.get("params", {}).get("sessionId") == session_id
                and item.get("params", {}).get("event", {}).get("type") == "turn/end"
            ]
            report["turn_reason"] = ends[-1]["data"]["reason"] if ends else None
            if not ends or ends[-1]["data"]["reason"]["kind"] != "completed":
                raise RuntimeError("Live author turn did not complete normally")
            task_path = Path(restored["directory"]) / "task.json"
            task = json.loads(task_path.read_text())
            submitted = task["workflow"]["rounds"][-1].get("submittedId")
            if not submitted:
                raise RuntimeError("Live author did not submit a real candidate")
            report["submitted_candidate"] = submitted
            report["model_turn_completed"] = True
            report["candidate_count"] = len(task["candidates"])
            report["new_candidate_count"] = len(
                task["workflow"]["rounds"][-1]["candidateIds"]
            )
            report["submitted_source_path"] = str(
                Path(restored["directory"]) / "candidates" / submitted / "shader.frag"
            )
            report["submitted_image_path"] = str(
                Path(restored["directory"]) / "candidates" / submitted / "render.png"
            )
            if args.free_task and len(task["candidates"]) < args.min_candidates:
                raise RuntimeError(
                    "History preparation produced fewer candidates than requested"
                )
        report["shutdown"] = rpc.request("shutdown", {}, time.monotonic() + 30)
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        if rpc is not None:
            try:
                if rpc.process.poll() is None and "shutdown" not in report:
                    report["shutdown"] = rpc.request(
                        "shutdown", {}, time.monotonic() + 15
                    )
                rpc.process.wait(timeout=15)
            except Exception as error:
                report["status"] = "failed"
                report["cleanup_error"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                }
                try:
                    rpc.close()
                except Exception as force_error:
                    report["cleanup_error"]["force_close_error"] = str(force_error)
            else:
                try:
                    rpc.close_streams()
                except Exception as error:
                    report["status"] = "failed"
                    report["cleanup_error"] = {
                        "type": type(error).__name__,
                        "message": str(error),
                    }
            report["events"] = rpc.notifications
            report["stderr"] = rpc.stderr
            report["responses"] = rpc.responses
        report["finished_at"] = timestamp()
        task_file = output / "business/restore-probe/task.json"
        if task_file.exists():
            final_task = json.loads(task_file.read_text())
            report["candidate_count"] = len(final_task.get("candidates", []))
            report["new_candidate_count"] = len(
                final_task.get("workflow", {})
                .get("rounds", [{}])[-1]
                .get("candidateIds", [])
            )
        audit_file = output / "http-audit.jsonl"
        if audit_file.exists():
            audit_rows = [
                json.loads(line)
                for line in audit_file.read_text().splitlines()
                if line.strip()
            ]
            report["model_budget_exhausted"] = any(
                row.get("message") == "Model request budget exceeded"
                for row in audit_rows
            )
        if checkpoint:
            report["template_unchanged"] = (
                file_manifest(Path(checkpoint["vistaRoot"]))
                == checkpoint["vistaManifest"]
            )
            if not report["template_unchanged"]:
                report["status"] = "failed"
                report["template_error"] = "Frozen VISTA template changed"
        save_report(output / "probe.json", report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "output": str(output),
                "error": report.get("error"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["probe", "checkpoint"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--business-root", type=Path, default=BUSINESS)
    parser.add_argument(
        "--reference", type=Path, default=BUSINESS / "images/pink_circle.png"
    )
    parser.add_argument("--cli", default=DEFAULT_CLI)
    parser.add_argument("--provider", default="deepseek-account")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--model-turn", action="store_true")
    parser.add_argument("--mcp-modes", action="store_true")
    parser.add_argument("--image-control", action="store_true")
    parser.add_argument(
        "--image-condition", choices=["visual", "text"], default="visual"
    )
    parser.add_argument("--max-model-requests", type=int, default=12)
    parser.add_argument("--max-list-calls", type=int, default=2)
    parser.add_argument("--max-read-calls", type=int, default=8)
    parser.add_argument("--free-task", action="store_true")
    parser.add_argument("--min-candidates", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--seed-source", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument(
        "--access-view", choices=["known", "discovery"], default="known"
    )
    parser.add_argument(
        "--condition", choices=["natural", "reminded", "provided"], default="natural"
    )
    parser.add_argument("--provided-records", nargs="*", default=[])
    parser.add_argument("--max-render-attempts", type=int)
    parser.add_argument("--generation-task", action="store_true")
    parser.add_argument("--requirements")
    parser.add_argument("--image-access-check", action="store_true")
    parser.add_argument("--audit-record")
    parser.add_argument("--experimental-host-cue", type=Path)
    parser.add_argument(
        "--native-author",
        action="store_true",
        help="Use the formal T05 author and omit the experimental budget reminder",
    )
    parser.add_argument(
        "--native-revision", help="Pin the native author to a full commit SHA"
    )
    parser.add_argument("--focused-run", choices=["011", "012"])
    parser.add_argument(
        "--business-feedback",
        help="Replay one literal feedback through the host continuation",
    )
    args = parser.parse_args()
    if args.native_revision and not args.native_author:
        parser.error("--native-revision requires --native-author")
    if args.native_author and (
        args.command != "checkpoint"
        or not args.business_feedback
        or args.experimental_host_cue
        or args.condition != "natural"
        or args.access_view != "discovery"
        or args.provided_records
        or args.image_access_check
        or args.mcp_modes
        or args.focused_run
    ):
        parser.error(
            "native author runs require unmodified business feedback and discovery"
        )
    if args.command == "checkpoint":
        if args.checkpoint is None:
            parser.error("checkpoint requires --checkpoint")
        args.model_turn = True
        args.image_control = True
    if args.focused_run:
        from shader_recall_followup import check_launch

        check_launch(args)
    if args.generation_task:
        args.model_turn = True
        args.image_control = True
        args.requirements = (
            args.requirements
            or "根据参考图实现过程式 WebGL2 Shader，尽量还原布局、轮廓、材质和色彩。"
        )
    if args.max_render_attempts is None:
        args.max_render_attempts = (
            max(10, args.min_candidates + 4) if args.free_task else 3
        )
    if (
        min(
            args.max_model_requests,
            args.max_render_attempts,
            args.max_list_calls,
            args.max_read_calls,
            args.min_candidates,
            args.timeout,
        )
        <= 0
    ):
        parser.error("budgets must be positive")
    return probe(args)


if __name__ == "__main__":
    raise SystemExit(main())
