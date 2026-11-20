"""Resolve experiment inputs from the manifest being used for a run or replay."""

import json
import os
from pathlib import Path

CASE_PATHS = ("root", "target_path", "current_path", "truth_path", "case_path")


def load_dataset(manifest: Path) -> dict:
    directory = manifest.resolve().parent
    data = json.loads(manifest.read_text())
    parent_manifest = data.get("encoding_repair", {}).get("parent_manifest")
    parent_dataset = Path(parent_manifest).parent.name if parent_manifest else None
    for case in data["cases"]:
        for field in CASE_PATHS:
            path = Path(case[field])
            if path.is_absolute():
                # Legacy v3 reuses v2 history and most v2 common inputs. Both
                # datasets live beside each other in the experiment archive.
                if path.parent.name != case["id"]:
                    raise ValueError(f"Unrecognized legacy {field} for {case['id']}")
                dataset = path.parents[1].name
                base = (
                    directory.parent / dataset
                    if dataset == parent_dataset
                    else directory
                )
                path = base / case["id"] / path.name
            case[field] = str((directory / path).resolve())
    return data


def relative_case(case: dict, directory: Path) -> dict:
    return {
        **case,
        **{
            key: os.path.relpath(case[key], directory)
            for key in CASE_PATHS
            if key in case
        },
    }
