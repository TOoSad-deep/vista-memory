"""Inject a filesystem failure into the real bridge or read back its store."""

import base64
import json
import os
import runpy
import sys
from pathlib import Path

from vista_memory import VistaStore


def obstruct_commit(stage, log_path):
    def audit(event, args):
        # Python emits os.link/os.rename immediately before the native call.
        # Remove the staged file so the real publication fails with ENOENT.
        if event != ("os.link" if stage == "save" else "os.rename"):
            return
        source, target, source_fd, _ = args
        name = Path(os.fsdecode(target)).name
        if stage == "save":
            if not (name.startswith("record-") and name.endswith(".json")):
                return
        elif name != "vista-records.json":
            return
        os.unlink(source, dir_fd=None if source_fd == -1 else source_fd)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"{event} {name}\n")

    sys.addaudithook(audit)


def read_records(root):
    store = VistaStore(root)
    rows = []
    for entry in store.list_records()["records"]:
        value = store.read_record(entry["record"], content="both")
        rows.append(
            {
                "record": entry["record"],
                "png": base64.b64encode(value.image).decode("ascii"),
                "source": value.code,
                "render": value.metadata.get("render"),
            }
        )
    return rows


if __name__ == "__main__":
    if sys.argv[1] == "read":
        print(json.dumps(read_records(sys.argv[2])))
    else:
        _, _, stage, log, bridge = sys.argv
        obstruct_commit(stage, Path(log))
        sys.argv = [bridge]
        sys.path[0] = str(Path(bridge).parent)
        runpy.run_path(bridge, run_name="__main__")
