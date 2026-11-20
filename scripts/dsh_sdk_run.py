"""Run isolated DeepSeek Harness SDK acceptance cases over JSON-RPC stdio."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import selectors
import signal
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_CLI = (
    "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
)


def timestamp() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def save_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".dsh-run-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class RpcProcess:
    """Read complete JSON lines without blocking on a partially written line."""

    def __init__(self, command: list[str], home: Path, workspace: Path):
        environment = os.environ.copy()
        environment["DSH_HOME"] = str(home)
        self.process = subprocess.Popen(
            command,
            cwd=workspace,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=0,
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ, "stdout")
        self.selector.register(self.process.stderr, selectors.EVENT_READ, "stderr")
        self.buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self.notifications: list[dict] = []
        self.stderr: list[str] = []
        self.responses: list[dict] = []
        self.pending: dict[int, dict] = {}
        self.next_id = 1

    def accept_line(self, stream: str, line: bytes) -> None:
        if stream == "stderr":
            self.stderr.append(line.decode("utf-8", errors="replace"))
            return
        if not line.strip():
            return
        try:
            message = json.loads(line)
        except (ValueError, UnicodeDecodeError) as error:
            raise RuntimeError("DSH stdout contains an invalid JSON line") from error
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise RuntimeError("DSH stdout contains an invalid JSON-RPC object")
        if "method" in message:
            if "id" in message:
                raise RuntimeError("DSH sent an unsupported server-to-client request")
            self.notifications.append(message)
            return
        if "id" not in message or ("result" in message) == ("error" in message):
            raise RuntimeError("DSH returned an invalid JSON-RPC response")
        self.responses.append(message)
        self.pending[message["id"]] = message

    def read_available(self, timeout: float) -> None:
        for key, _ in self.selector.select(timeout):
            data = os.read(key.fileobj.fileno(), 65536)
            buffer = self.buffers[key.data]
            buffer.extend(data)
            while b"\n" in buffer:
                line, _, remaining = buffer.partition(b"\n")
                buffer[:] = remaining
                self.accept_line(key.data, line)
            if not data:
                self.selector.unregister(key.fileobj)
                if buffer:
                    self.accept_line(key.data, bytes(buffer))
                    buffer.clear()

    def request(self, method: str, params: dict, deadline: float) -> dict:
        request_id = self.next_id
        self.next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        payload = memoryview((json.dumps(request) + "\n").encode())
        while payload:
            written = self.process.stdin.write(payload)
            if not written:
                raise BrokenPipeError("DSH stdin closed while sending a request")
            payload = payload[written:]
        self.process.stdin.flush()
        while request_id not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"DSH timed out waiting for {method}")
            self.read_available(min(remaining, 0.5))
            if not self.selector.get_map() and request_id not in self.pending:
                raise RuntimeError(f"DSH exited before responding to {method}")
        response = self.pending.pop(request_id)
        if "error" in response:
            raise RuntimeError(f"DSH {method} failed: {response['error']}")
        return response

    def close_streams(self) -> None:
        try:
            self.read_available(0)
        finally:
            self.selector.close()
            for stream in (
                self.process.stdin,
                self.process.stdout,
                self.process.stderr,
            ):
                stream.close()

    def close(self) -> None:
        # Child MCP processes share the private group created by Popen.
        def signal_group(sig: int) -> None:
            try:
                os.killpg(self.process.pid, sig)
            except ProcessLookupError:
                pass

        try:
            signal_group(signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            signal_group(signal.SIGKILL)
            self.process.wait(timeout=5)
        finally:
            self.close_streams()


def append_record(specification: dict) -> dict:
    from vista_memory import VistaStore

    arguments = dict(specification)
    store = VistaStore(arguments.pop("root"))
    arguments["png"] = Path(arguments.pop("png_path")).read_bytes()
    return store.save_record(**arguments)


def prompt_images(specifications: list[dict]) -> tuple[list[dict], list[dict]]:
    """Prepare SDK image blocks and a byte-free, reproducible input manifest."""
    blocks = []
    manifest = []
    for specification in specifications:
        path = Path(specification["path"]).expanduser().resolve()
        data = path.read_bytes()
        mime_type = specification.get("mime_type", "image/png")
        name = specification.get("name", path.name)
        blocks.append(
            {
                "type": "image",
                "data": base64.b64encode(data).decode("ascii"),
                "mimeType": mime_type,
                "name": name,
            }
        )
        manifest.append(
            {
                "path": str(path),
                "name": name,
                "mime_type": mime_type,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return blocks, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("home", "overlay", "workspace", "cases", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--cli", default=DEFAULT_CLI)
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--reasoning", default="high")
    parser.add_argument("--max-tokens", type=int, default=8192)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text())
    if not isinstance(cases, list):
        parser.error("The cases file must contain a JSON array")
    for case in cases:
        if not isinstance(case, dict) or any(
            not isinstance(case.get(key), str) for key in ("id", "session_id", "text")
        ):
            parser.error("Every case requires string id, session_id, and text fields")
        if case.get("timeout_seconds", 300) <= 0:
            parser.error("Case timeout_seconds must be positive")
        images = case.get("images", [])
        if not isinstance(images, list) or any(
            not isinstance(image, dict) or not isinstance(image.get("path"), str)
            for image in images
        ):
            parser.error("Case images must be a list of objects with string path")
    home, overlay, workspace = (
        path.expanduser().resolve()
        for path in (args.home, args.overlay, args.workspace)
    )
    home.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    command = [args.cli, "--profile", "sdk-minimal", "--patch", str(overlay)]
    initialize = {
        "cwd": str(workspace),
        "provider": args.provider,
        "model": args.model,
        "reasoningEffort": args.reasoning,
        "maxTokens": args.max_tokens,
    }
    report = {
        "command": command,
        "home": str(home),
        "initialize_params": initialize,
        "started_at": timestamp(),
        "cases": [],
        "status": "running",
    }
    save_report(args.output, report)
    rpc = None
    active_case = None
    event_start = 0
    fatal = False
    stage = "initialize"
    try:
        rpc = RpcProcess(command, home, workspace)
        report["process_pid"] = rpc.process.pid
        report["initialize_response"] = rpc.request(
            "initialize", initialize, time.monotonic() + 120
        )
        for case in cases:
            stage = "prepare"
            event_start = len(rpc.notifications)
            active_case = {
                "id": case["id"],
                "session_id": case["session_id"],
                "started_at": timestamp(),
                "status": "running",
            }
            report["cases"].append(active_case)
            text = case["text"]
            if "append_record" in case:
                try:
                    summary = append_record(case["append_record"])
                except Exception as error:
                    active_case["status"] = "failed"
                    active_case["error"] = {
                        "type": type(error).__name__,
                        "message": str(error),
                    }
                    active_case["finished_at"] = timestamp()
                    active_case["events"] = []
                    save_report(args.output, report)
                    print(f"{case['id']}: failed", flush=True)
                    active_case = None
                    continue
                active_case["append_summary"] = summary
                text = text.replace("{{record}}", summary["record"])
            active_case["prompt"] = text
            image_blocks, active_case["prompt_images"] = prompt_images(
                case.get("images", [])
            )
            save_report(args.output, report)
            deadline = time.monotonic() + case.get("timeout_seconds", 300)
            stage = "prompt"
            active_case["prompt_response"] = rpc.request(
                "session/prompt",
                {
                    "sessionId": case["session_id"],
                    "contentBlocks": [{"type": "text", "text": text}, *image_blocks],
                },
                deadline,
            )
            stage = "wait"
            active_case["wait_response"] = rpc.request(
                "session/wait", {"sessionId": case["session_id"]}, deadline
            )
            active_case["events"] = rpc.notifications[event_start:]
            turn_ends = [
                message["params"]["event"]
                for message in active_case["events"]
                if message.get("method") == "session.event"
                and message.get("params", {}).get("sessionId") == case["session_id"]
                and message.get("params", {}).get("event", {}).get("type") == "turn/end"
            ]
            active_case["turn_end"] = turn_ends[-1] if turn_ends else None
            completed = bool(
                turn_ends
                and turn_ends[-1].get("data", {}).get("reason", {}).get("kind")
                == "completed"
            )
            active_case["status"] = "completed" if completed else "failed"
            active_case["finished_at"] = timestamp()
            report["stderr"] = rpc.stderr
            report["responses"] = rpc.responses
            save_report(args.output, report)
            print(f"{case['id']}: {active_case['status']}", flush=True)
            active_case = None
        stage = "shutdown"
        report["shutdown_response"] = rpc.request("shutdown", {}, time.monotonic() + 30)
        rpc.process.wait(timeout=10)
    except (Exception, KeyboardInterrupt) as error:
        fatal = True
        report["fatal_error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "stage": stage,
        }
        if active_case is not None:
            active_case["status"] = "failed"
            active_case["error"] = report["fatal_error"]
            active_case["finished_at"] = timestamp()
            active_case["events"] = rpc.notifications[event_start:] if rpc else []
        print(f"SDK run failed: {type(error).__name__}", flush=True)
    finally:
        if rpc is not None:
            try:
                rpc.close()
            except Exception as error:
                fatal = True
                report["cleanup_error"] = str(error)
            report["stderr"] = rpc.stderr
            report["responses"] = rpc.responses
            report["process_exit_code"] = rpc.process.returncode
        passed = not fatal and all(
            case["status"] == "completed" for case in report["cases"]
        )
        report["status"] = "completed" if passed else "failed"
        report["finished_at"] = timestamp()
        save_report(args.output, report)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
