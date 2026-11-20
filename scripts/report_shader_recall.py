"""Export auditable per-run and paired summaries from scored recall batches."""

from __future__ import annotations

import argparse
import csv
import json
import random
from collections import defaultdict
from pathlib import Path
from statistics import mean


def boolean_metric(rows: list[dict], field: str) -> dict:
    values = [row.get(field) for row in rows]
    known = [value for value in values if isinstance(value, bool)]
    return {
        "positive": sum(known),
        "known": len(known),
        "all_runs": len(values),
        "rate_among_known": mean(known) if known else None,
    }


def paired(rows: list[dict], left: str, right: str, selector: str) -> dict:
    groups = defaultdict(dict)
    for row in rows:
        if row[selector] in {left, right}:
            key = (row["task_id"], row["repeat"], row["condition"])
            if selector == "condition":
                key = (row["task_id"], row["repeat"], row["access_view"])
            groups[key][row[selector]] = row
    pairs = [group for group in groups.values() if left in group and right in group]
    result = {"left": left, "right": right, "pairs": len(pairs), "metrics": {}}
    for metric in [
        "author_submitted",
        "recalled_in_run",
        "evidence_visible_in_run",
        "model_requests",
        "wall_seconds",
    ]:
        by_task = defaultdict(list)
        for group in pairs:
            a, b = group[left], group[right]
            if a.get(metric) is not None and b.get(metric) is not None:
                by_task[a["task_id"]].append(float(b[metric]) - float(a[metric]))
        task_values = [mean(values) for values in by_task.values()]
        if not task_values:
            continue
        rng = random.Random(20261117)
        samples = sorted(
            mean(rng.choices(task_values, k=len(task_values))) for _ in range(5000)
        )
        result["metrics"][metric] = {
            "right_minus_left_task_mean": mean(task_values),
            "task_clusters": len(task_values),
            "cluster_bootstrap_95_interval": [samples[124], samples[4874]],
        }
    return result


def export(batch: Path) -> dict:
    summary = json.loads((batch / "summary.json").read_text())
    schedule = json.loads((batch / "schedule.json").read_text())
    rows = []
    for result, scheduled in zip(summary["results"], schedule, strict=False):
        trace = result.get("trace", {})
        outcome = result.get("outcome", {})
        cost = result.get("cost", {})
        case = scheduled["case"]
        rows.append(
            {
                "index": result["index"],
                "case_id": result["case_id"],
                "task_id": case.get("task_id", case["id"]),
                "purpose": case.get("purpose", "end-to-end"),
                "repeat": result["repeat"],
                "condition": result["condition"],
                "access_view": result["access_view"],
                "image_condition": result["image_condition"],
                "status": result["status"],
                "author_submitted": outcome.get("author_submission_completed", False),
                "parameter_objective_met": outcome.get("functional_objective_met"),
                "recalled_in_run": trace.get("recall_attempted_in_run"),
                "evidence_visible_in_run": trace.get(
                    "required_evidence_visible_in_run"
                ),
                "triggered_before_decision": trace.get(
                    "recall_triggered_before_decision"
                ),
                "timely_evidence": trace.get("timely_related_evidence"),
                "timing_status": trace.get("status"),
                "vista_read_calls": trace.get("vista_read_calls"),
                "vista_reads": trace.get("vista_reads"),
                "other_image_reads": trace.get("other_image_reads"),
                "list_calls": trace.get("list_calls"),
                "model_requests": result.get("model_requests"),
                "wall_seconds": cost.get("wall_seconds"),
                "input_tokens": cost.get("usage", {}).get("inputTokens"),
                "output_tokens": cost.get("usage", {}).get("outputTokens"),
                "cache_read_tokens": cost.get("usage", {}).get("cacheReadTokens"),
                "visual_acceptance": outcome.get("visual_acceptance", "pending-review"),
                "directory": result["directory"],
            }
        )
    with (batch / "summary.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    grouped = defaultdict(list)
    for row in rows:
        key = (
            row["purpose"],
            row["condition"],
            row["access_view"],
            row["image_condition"],
        )
        grouped[key].append(row)
    groups = []
    for key, members in sorted(grouped.items()):
        groups.append(
            {
                "purpose": key[0],
                "condition": key[1],
                "access_view": key[2],
                "image_condition": key[3],
                "runs": len(members),
                **{
                    field: boolean_metric(members, field)
                    for field in [
                        "author_submitted",
                        "parameter_objective_met",
                        "recalled_in_run",
                        "evidence_visible_in_run",
                        "triggered_before_decision",
                        "timely_evidence",
                    ]
                },
                "mean_model_requests": mean(row["model_requests"] for row in members),
                "mean_wall_seconds": mean(row["wall_seconds"] for row in members),
            }
        )
    opportunities = [row for row in rows if row["purpose"] == "opportunity"]
    generations = [row for row in rows if row["purpose"] == "end-to-end"]
    analysis = {
        "execution_status": summary["execution_status"],
        "runs": len(rows),
        "planned_runs": summary["planned_runs"],
        "original_scheduled_runs": len(schedule),
        "scope_execution_status": summary.get("scope_execution_status"),
        "groups": groups,
        "view_pairs": paired(opportunities, "known", "discovery", "access_view"),
        "reminder_pairs": paired(opportunities, "natural", "reminded", "condition"),
        "image_condition_pairs": paired(
            generations, "text", "visual", "image_condition"
        ),
        "quality_effect": "pending-visual-review",
        "clustering": (
            "Each PNG task has equal weight after averaging its repeats and views."
        ),
    }
    (batch / "analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2) + "\n"
    )
    return analysis


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    args = parser.parse_args()
    result = export(args.batch)
    print(
        json.dumps(
            {
                key: result[key]
                for key in [
                    "execution_status",
                    "runs",
                    "planned_runs",
                    "quality_effect",
                ]
            }
        )
    )


if __name__ == "__main__":
    main()
