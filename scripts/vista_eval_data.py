"""Generate frozen synthetic VISTA comparison and decision cases."""

from __future__ import annotations

import argparse
import hashlib
import io
import itertools
import json
import math
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageStat
from PIL.PngImagePlugin import PngInfo
from vista_eval_paths import relative_case

from vista_memory import VistaStore

BACKGROUND = (19, 27, 42)
BLUE = (48, 156, 220)
GOLD = (224, 177, 70)
WIDTH, HEIGHT = 192, 160
FAMILIES = ("area", "position", "spacing", "brightness")
FEATURE_NAMES = {
    "area": "visible blue circle area in pixels",
    "position": "horizontal centroid of the blue circle in pixels",
    "spacing": "mean center-to-center spacing of blue vertical stripes in pixels",
    "brightness": "gray disk brightness on a 0 to 255 scale",
}


def scene_spec(scene_id: str) -> dict:
    """Fixed program constants selected by an externally recorded scene id."""
    number = int(scene_id[1:])
    family = FAMILIES[(number - 1) % 4]
    group = (number - 1) // 4
    return {
        "family": family,
        "midpoint": (2.25, 2.7, 1.65, 3.15)[(number - 1) % 4] + group * 0.13,
        "slope": 1.15 + group * 0.09,
        "reverse": family == "brightness" and group % 2 == 1,
        "landmark_x": 108 + group * 3,
        "landmark_y": 72 + group * 2,
        "minimum": {"area": 12, "position": 28, "spacing": 8, "brightness": 35}[family],
        "maximum": {"area": 60, "position": 162, "spacing": 30, "brightness": 225}[
            family
        ],
    }


def render_case(
    case: dict, control: float, target_variant: bool = False
) -> Image.Image:
    """Execute the fixed drawing program at one scalar control value."""
    spec = scene_spec(case["scene_id"])
    width, height = case["width"], case["height"]
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    fraction = 1 / (1 + math.exp(-spec["slope"] * (control - spec["midpoint"])))
    if spec["reverse"]:
        fraction = 1 - fraction
    value = spec["minimum"] + (spec["maximum"] - spec["minimum"]) * fraction
    family = spec["family"]
    if family == "area":
        radius = round(value)
        center = (width // 2, height // 2)
        draw.ellipse(
            (
                center[0] - radius,
                center[1] - radius,
                center[0] + radius,
                center[1] + radius,
            ),
            fill=BLUE,
        )
        x = spec["landmark_x"]
        draw.rectangle((x, 18, x + 9, height - 18), fill=GOLD)
    elif family == "position":
        center_x, center_y, radius = round(value), height // 2, 21
        draw.ellipse(
            (
                center_x - radius,
                center_y - radius,
                center_x + radius,
                center_y + radius,
            ),
            fill=BLUE,
        )
        x = spec["landmark_x"] - 12
        draw.rectangle((x, 20, x + 7, height - 20), fill=GOLD)
    elif family == "spacing":
        draw.rectangle((22, 35, width - 22, height - 35), outline=GOLD, width=2)
        stripe_x = 30.0
        while stripe_x < width - 28:
            x = round(stripe_x)
            draw.rectangle((x, 42, x + 3, height - 42), fill=BLUE)
            stripe_x += value
    else:
        gray = round(value)
        radius = 40 + (int(case["scene_id"][1:]) // 4)
        draw.ellipse(
            (
                width // 2 - radius,
                height // 2 - radius,
                width // 2 + radius,
                height // 2 + radius,
            ),
            fill=(gray, gray, gray),
        )
        draw.rectangle((12, 12, 24, 24), fill=(128, 128, 128))
        draw.rectangle((width - 25, 12, width - 13, 24), outline=GOLD, width=2)
    if target_variant:
        # An irrelevant corner mark makes target bytes distinct from history.
        draw.rectangle((width - 12, height - 12, width - 7, height - 7), fill=GOLD)
    return image


def image_metric(case: dict, image: Image.Image) -> float:
    """Measure the requested feature from actual rendered pixels."""
    family = scene_spec(case["scene_id"])["family"]
    width, height = image.size
    pixels = image.load()
    if family == "brightness":
        return float(pixels[width // 2, height // 2][0])
    if family == "spacing":
        centers = []
        run_start = None
        for x in range(width):
            blue = pixels[x, height // 2] == BLUE
            if blue and run_start is None:
                run_start = x
            if not blue and run_start is not None:
                centers.append((run_start + x - 1) / 2)
                run_start = None
        return sum(b - a for a, b in zip(centers, centers[1:])) / (len(centers) - 1)
    xs = [x for y in range(height) for x in range(width) if pixels[x, y] == BLUE]
    if family == "area":
        return float(len(xs))
    return sum(xs) / len(xs)


def pixel_error(left: Image.Image, right: Image.Image) -> float:
    """Mean absolute channel error for the actual action-rendered images."""
    return sum(ImageStat.Stat(ImageChops.difference(left, right)).mean) / 3


def png_bytes(image: Image.Image, common_marker: str | None = None) -> bytes:
    output = io.BytesIO()
    metadata = None
    if common_marker is not None:
        metadata = PngInfo()
        metadata.add_text("vista-eval-common", common_marker)
    image.save(output, format="PNG", pnginfo=metadata)
    return output.getvalue()


def caption(case: dict, image: Image.Image, detailed: bool) -> str:
    """Question-independent observations, shared by both experiment conditions."""
    family = scene_spec(case["scene_id"])["family"]
    metric = image_metric(case, image)
    if family == "area":
        size = "small" if metric < 1800 else "medium" if metric < 5500 else "large"
        text = (
            f"A {size} blue circle is centered on a dark navy background. "
            "A fixed vertical gold bar stands right of center and obscures the "
            "circle where they overlap."
        )
    elif family == "position":
        position = "left" if metric < 75 else "middle" if metric < 125 else "right"
        text = (
            f"A blue circle lies in the {position} part of a dark navy image. "
            "A fixed vertical gold bar can obscure the circle where they overlap."
        )
    elif family == "spacing":
        density = "dense" if metric < 14 else "moderate" if metric < 23 else "sparse"
        text = (
            f"A gold rectangular frame contains {density} blue vertical stripes "
            "against a dark navy background."
        )
    else:
        brightness = "dark" if metric < 95 else "mid-gray" if metric < 165 else "light"
        text = (
            f"A {brightness} gray disk is centered on a dark navy background. "
            "A fixed gray square at upper left is a brightness reference."
        )
    if detailed:
        text += f" Measured {FEATURE_NAMES[family]} is {metric:.4f}."
    return text


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def build_case(output: Path, definition: dict, rng: random.Random) -> dict:
    case_id = definition["id"]
    directory = output / case_id
    directory.mkdir()
    case = {
        **definition,
        "width": WIDTH,
        "height": HEIGHT,
        "root": str((directory / "vista").resolve()),
        "target_path": str((directory / "target.png").resolve()),
        "current_path": str((directory / "current.png").resolve()),
        "record_order": [],
        "summary_rows": [],
        "current_control": definition.get("current_control", 2.0),
        "action_step": 1.0,
    }
    store = VistaStore(case["root"])
    metrics = []
    hashes = {}
    controls = [float(number) for number in range(6)]
    for index, control in enumerate(controls):
        image = render_case(case, control)
        png = png_bytes(image)
        render = {
            "scene_id": case["scene_id"],
            "control": control,
            "width": WIDTH,
            "height": HEIGHT,
        }
        summary = store.save_record(
            png=png,
            label=f"sample-{index + 1}",
            source_agent="evaluation",
            render=render,
        )
        record = summary["record"]
        case["record_order"].append(record)
        case["summary_rows"].append(
            {
                "record": record,
                "control": control,
                "summary": caption(case, image, case["caption_detail"] == "detailed"),
                "render": render,
            }
        )
        metrics.append(image_metric(case, image))
        hashes[record] = hashlib.sha256(png).hexdigest()

    family = scene_spec(case["scene_id"])["family"]
    truth = {
        "case_id": case_id,
        "scene_family": family,
        "metrics": metrics,
        "feature_name": FEATURE_NAMES[family],
        "answer": {"record": None, "pair": [], "direction": None, "action": None},
        "evidence_sets": [],
        "png_sha256": hashes,
    }
    task = case["task_type"]
    if task == "R1":
        selected = rng.choice([1, 2, 3, 4])
        target_control = controls[selected]
        target = render_case(case, target_control, target_variant=True)
        errors = [
            pixel_error(render_case(case, control, target_variant=True), target)
            for control in controls
        ]
        best = min(range(6), key=errors.__getitem__)
        assert best == selected and sum(error == errors[best] for error in errors) == 1
        truth["answer"]["record"] = case["record_order"][best]
        truth["answer"]["evidence"] = {"feature": family, "value": metrics[best]}
        truth["evidence_sets"] = [[case["record_order"][best]]]
        truth["comparison_errors"] = errors
        truth["unique_margin"] = sorted(errors)[1] - sorted(errors)[0]
        task_text = (
            "Choose the one historical record whose foreground appearance and "
            "relationship to the fixed gold or gray landmark match the TARGET image. "
            "Compare the circle silhouette and overlap, stripe spacing, or disk "
            "brightness as appropriate. Ignore the small gold mark at the bottom-right "
            "corner of the TARGET. Cite the selected record as evidence."
        )
    elif task == "R2":
        gaps = [abs(b - a) for a, b in zip(metrics, metrics[1:])]
        largest = max(range(5), key=gaps.__getitem__)
        assert sum(math.isclose(gap, gaps[largest]) for gap in gaps) == 1, gaps
        next_control = controls[largest + 1] + 0.5
        next_metric = image_metric(case, render_case(case, next_control))
        difference = next_metric - metrics[largest + 1]
        assert abs(difference) > 1e-6
        pair = case["record_order"][largest : largest + 2]
        truth["answer"]["pair"] = pair
        truth["answer"]["direction"] = "increase" if difference > 0 else "decrease"
        truth["answer"]["evidence"] = {"feature": family, "delta": gaps[largest]}
        truth["evidence_sets"] = [pair]
        truth["adjacent_gaps"] = gaps
        truth["unique_margin"] = sorted(gaps)[-1] - sorted(gaps)[-2]
        truth["local_prediction"] = {"control": next_control, "metric": next_metric}
        target_control = controls[-1]
        target = render_case(case, target_control, target_variant=True)
        task_text = (
            f"The measured feature is {FEATURE_NAMES[family]}. The ordered history "
            "contains equal +1 changes in control. Other external inputs stay fixed. "
            "Identify the adjacent pair with the largest absolute feature change. "
            "Then predict whether the feature will increase or decrease if control is "
            "increased by 0.5 immediately after the second record of that pair. "
            "Cite both records. The TARGET shows the last historical state with an "
            "irrelevant bottom-right mark; the CURRENT shows the starting state."
        )
        case["current_control"] = controls[0]
    else:
        target_action = definition["target_action"]
        current_control = case["current_control"]
        action_controls = {
            "decrease": current_control - 1,
            "hold": current_control,
            "increase": current_control + 1,
        }
        target_control = action_controls[target_action]
        target = render_case(case, target_control)
        errors = {
            action: pixel_error(render_case(case, control), target)
            for action, control in action_controls.items()
        }
        best = min(errors, key=errors.__getitem__)
        assert best == target_action
        assert sum(error == errors[best] for error in errors.values()) == 1
        truth["answer"]["action"] = best
        truth["answer"]["evidence"] = {
            "feature": family,
            "target_metric": image_metric(case, target),
        }
        truth["action_errors"] = errors
        truth["unique_margin"] = sorted(errors.values())[1] - sorted(errors.values())[0]
        for left, right in itertools.combinations(range(6), 2):
            if controls[left] <= current_control <= controls[right]:
                truth["evidence_sets"].append(
                    [case["record_order"][left], case["record_order"][right]]
                )
        case["action_options"] = action_controls
        task_text = (
            f"The CURRENT image was rendered at control={current_control:g}. Choose "
            "decrease (control - 1), hold, or increase (control + 1) to make the next "
            "render closest to the TARGET. Compare the history and both images. "
            "Cite two distinct historical records that bracket the current control, "
            "one at or below it and one at or above it. The chosen action will be "
            "executed and scored using mean absolute pixel error against the TARGET."
        )
    truth["target_control"] = target_control
    current = render_case(case, case["current_control"])
    for name, image in (("target", target), ("current", current)):
        png = png_bytes(image, common_marker=f"{case_id}/{name}")
        Path(case[f"{name}_path"]).write_bytes(png)
        common_hash = hashlib.sha256(png).hexdigest()
        assert common_hash not in hashes.values()
        assert Image.open(io.BytesIO(png)).tobytes() == image.tobytes()
        truth[f"{name}_sha256"] = common_hash
    # These choices belong only to the private scoring side.
    case.pop("target_action", None)
    case["prompt"] = (
        "You are evaluating a recorded deterministic visual process. The two attached "
        "images are TARGET first and CURRENT second. History captions were produced "
        "by a fixed, question-independent observation rule. All historical external "
        "render inputs are included below. GLSL is null because these images were "
        "produced by a fixed drawing program. The internal drawing function stays "
        "constant within this case. Use list_records/read_record to inspect history "
        "when helpful. You may call list_records at most twice and read_record at most "
        "eight times.\n\n"
        + task_text
        + "\n\nThe history is ordered by the following table, not by filenames:\n"
        + json.dumps(case["summary_rows"], ensure_ascii=False)
        + '\n\nReturn one JSON object with these keys: {"record":null,"pair":[], '
        '"direction":null,"action":null,"evidence_records":[],"evidence":{}}. '
        "Fill record for selection, pair and direction for change analysis, or action "
        "for action selection. Use exact record filenames. The evidence object may "
        "state the observed feature and measurements. Provide a best-supported answer "
        "using the available observations."
    )
    write_json(directory / "case.json", relative_case(case, directory))
    write_json(directory / "truth.json", truth)
    return relative_case(
        {
            **case,
            "case_path": str((directory / "case.json").resolve()),
            "truth_path": str((directory / "truth.json").resolve()),
            "scene_family": family,
        },
        output,
    )


def build_dataset(output: Path, seed: int) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    definitions = []
    for task_index, task in enumerate(("R1", "R2", "R3")):
        for index in range(4):
            definition = {
                "id": f"{task}-{index + 1:02d}",
                "task_type": task,
                "split": "formal",
                "scenario": "new_session" if index < 2 else "compacted_context",
                "scene_id": f"s{task_index * 4 + index + 1:02d}",
                "caption_detail": "detailed" if index == 0 else "coarse",
            }
            if task == "R3":
                definition["current_control"] = (2.0, 3.0, 2.0, 3.0)[index]
                definition["target_action"] = (
                    "decrease",
                    "hold",
                    "increase",
                    "decrease",
                )[index]
            definitions.append(definition)
    definitions.extend(
        [
            {
                "id": "CAL-01",
                "task_type": "R2",
                "split": "calibration",
                "scenario": "new_session",
                "scene_id": "s14",
                "caption_detail": "detailed",
            },
            {
                "id": "CAL-02",
                "task_type": "R3",
                "split": "calibration",
                "scenario": "compacted_context",
                "scene_id": "s16",
                "caption_detail": "coarse",
                "current_control": 2.0,
                "target_action": "increase",
            },
        ]
    )
    rng = random.Random(seed)
    cases = [build_case(output, definition, rng) for definition in definitions]
    manifest = {
        "seed": seed,
        "encoding_revision": 2,
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "renderer": "Pillow deterministic drawing, four scene families",
        "record_ids": "Backend-generated unique filenames, frozen in this corpus",
        "context_scenarios": (
            "Both scenarios reconstruct task context. "
            "compacted_context simulates retained summaries."
        ),
        "cases": cases,
    }
    write_json(output / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20261116)
    arguments = parser.parse_args()
    manifest = build_dataset(arguments.output.resolve(), arguments.seed)
    print(
        json.dumps(
            {
                "cases": len(manifest["cases"]),
                "manifest": str(arguments.output / "manifest.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
