"""Verify a live historical-image access probe against its wire evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from vista_memory import VistaStore


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def validate(run: Path, record: str, condition: str) -> dict:
    probe = json.loads((run / "probe.json").read_text())
    tools = lines(run / "tool-image-control.jsonl")
    wire = lines(run / "http-audit.jsonl")
    requests = [row for row in wire if row.get("model")]
    errors = [
        row for row in lines(run / "http-audit.jsonl")
        if row["type"] == "image-control-error"
    ]
    content = VistaStore(run / "vista").read_record(record, content="code")
    code_hash = hashlib.sha256(content.code.encode()).hexdigest()
    metadata_hash = hashlib.sha256(
        json.dumps(content.metadata, ensure_ascii=False).encode()
    ).hexdigest()
    disposition = "withheld" if condition == "text" else "visible"
    old = [row for row in tools if row["origin"].get("record") == record]
    composites = [row for row in tools if row["origin"]["kind"] == "composite"]
    images = [image for row in requests for image in row.get("images", [])]
    checks = {
        "author_submitted": probe["status"] == "passed",
        "template_unchanged": probe.get("template_unchanged") is True,
        "no_control_failure": not errors
        and not (run / "image-control-failure.json").exists(),
        "mcp_historical_image_controlled": any(
            row["name"] == "mcp__vista__read_record"
            and row["disposition"] == disposition for row in old
        ),
        "filesystem_historical_image_controlled": any(
            row["name"] == "read_image" and row["disposition"] == disposition
            for row in old
        ),
        "historical_composite_controlled": any(
            row["disposition"] == ("masked" if condition == "text" else "visible")
            for row in composites
        ),
        "code_and_parameters_visible": any(
            code_hash in row.get("includedCodeSha256", [])
            and metadata_hash in row.get("includedMetadataSha256", [])
            for row in requests
        ),
        "no_unresolved_file_references": not any(
            image.get("unresolvedFile") for image in images
        ),
        "reference_visible": any(
            image.get("source", {}).get("kind") == "reference" for image in images
        ),
    }
    old_visible = any(
        image.get("source", {}).get("record") == record for image in images
    )
    checks["historical_wire_visibility_matches"] = (
        old_visible == (condition == "visual")
    )
    if condition == "text":
        checks["cached_old_current_controlled"] = any(
            item["disposition"] == "withheld"
            and item["source"].get("candidateId")
            for row in requests for item in row.get("imageControl", [])
        )
        checks["raw_historical_attachment_not_uploaded"] = all(
            image["sha256"] != row["originalAttachmentId"].removeprefix("sha256:")
            for row in old for image in wire if image["type"] == "image-upload"
        )
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "condition": condition, "record": record, "checks": checks,
        "model_requests": len(requests), "image_occurrences": len(images),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--record", required=True)
    parser.add_argument("--condition", choices=["text", "visual"], required=True)
    args = parser.parse_args()
    result = validate(args.run, args.record, args.condition)
    (args.run / "image-access-validation.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
