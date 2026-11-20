"""Observe image-file lineage in author Python commands during recall tests."""

import json
import os
import sys
import threading
import time

_destination = os.environ.get("DSH_RECALL_FS_AUDIT")
_run_root = os.environ.get("DSH_RECALL_FS_AUDIT_ROOT", "")
_guard = threading.local()
_started = time.time_ns()


def _observe(event, arguments):
    if event != "open" or not _destination or getattr(_guard, "active", False):
        return
    path, mode, flags = arguments
    if not isinstance(path, (str, bytes)):
        return
    path = os.fsdecode(path)
    image_file = path.lower().endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
    )
    local_data = _run_root and os.path.realpath(path).startswith(_run_root + os.sep)
    excluded = path.lower().endswith((".py", ".pyc", ".frag", ".glsl", ".md", ".jsonl"))
    if not image_file and not (local_data and not excluded):
        return
    _guard.active = True
    try:
        write = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        row = {
            "process": f"{os.getpid()}-{_started}",
            "time_ns": time.time_ns(),
            "path": os.path.realpath(path),
            "operation": "write" if write else "read",
        }
        descriptor = os.open(
            _destination, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        try:
            os.write(descriptor, (json.dumps(row) + "\n").encode())
        finally:
            os.close(descriptor)
    finally:
        _guard.active = False


if _destination:
    sys.addaudithook(_observe)
