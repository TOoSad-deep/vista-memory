"""Verify the archived experiment files without running a model."""

import argparse
import hashlib
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def verify_freeze(path: Path, *, archive: bool = False, repo: Path = REPO) -> int:
    entries = json.loads(path.read_text())["sha256"]
    original_root = next(
        Path(name).parent for name in entries if Path(name).name == "pyproject.toml"
    )
    copies = path.parent / "frozen-execution-scripts"
    index = json.loads((copies / "index.json").read_text()) if archive else {}
    for filename, expected in entries.items():
        relative = Path(filename).relative_to(original_root)
        entry = index.get(relative.as_posix())
        src = repo / relative
        if entry is not None:
            if entry["sha256"] != expected:
                raise ValueError(f"Archive index differs from freeze: {relative}")
            src = copies / entry["archive"]
        if hashlib.sha256(src.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Frozen input changed: {src}")
    return len(entries)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("freeze", type=Path, nargs="+")
    for manifest in parser.parse_args().freeze:
        print(
            f"{manifest.name}: verified {verify_freeze(manifest, archive=True)} files"
        )
