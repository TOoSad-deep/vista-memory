"""Re-encode selected common inputs losslessly while preserving task pixels."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path

from PIL import Image
from vista_eval_paths import load_dataset, relative_case


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case-ids", required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = load_dataset(args.dataset)
    requested = set(args.case_ids.split(","))
    assert requested <= {case["id"] for case in manifest["cases"]}
    changes = []
    for case in manifest["cases"]:
        if case["id"] not in requested:
            continue
        directory = output / case["id"]
        directory.mkdir()
        truth = json.loads(Path(case["truth_path"]).read_text())
        forbidden = set(truth["png_sha256"].values())
        for role, levels in (("target", [9, 1, 0]), ("current", [1, 9, 0])):
            original_path = Path(case[f"{role}_path"])
            with Image.open(original_path) as source:
                original = source.convert("RGB")
                clean = Image.frombytes("RGB", original.size, original.tobytes())
            for level in levels:
                stream = io.BytesIO()
                clean.save(stream, format="PNG", compress_level=level)
                data = stream.getvalue()
                digest = hashlib.sha256(data).hexdigest()
                if digest not in forbidden:
                    break
            else:
                raise ValueError(f"No distinct encoding for {case['id']} {role}")
            with Image.open(io.BytesIO(data)) as decoded:
                assert decoded.convert("RGB").tobytes() == original.tobytes()
            destination = directory / f"{role}.png"
            destination.write_bytes(data)
            forbidden.add(digest)
            changes.append(
                {
                    "case_id": case["id"],
                    "role": role,
                    "original_path": os.path.relpath(original_path, output),
                    "path": os.path.relpath(destination, output),
                    "compression_level": level,
                    "sha256": digest,
                    "pixels_identical": True,
                }
            )
            case[f"{role}_path"] = str(destination)
            truth[f"{role}_sha256"] = digest
        case["case_path"] = str(directory / "case.json")
        case["truth_path"] = str(directory / "truth.json")
        (directory / "case.json").write_text(
            json.dumps(relative_case(case, directory), indent=2) + "\n"
        )
        (directory / "truth.json").write_text(json.dumps(truth, indent=2) + "\n")
    manifest["cases"] = [relative_case(case, output) for case in manifest["cases"]]
    manifest["encoding_revision"] = 3
    manifest["encoding_repair"] = {
        "parent_manifest": os.path.relpath(args.dataset.resolve(), output),
        "case_ids": sorted(requested),
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "changes": changes,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"manifest": str(output / "manifest.json"), "changes": changes}))


if __name__ == "__main__":
    main()
