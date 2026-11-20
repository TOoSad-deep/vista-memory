"""Shader benchmark bridge: delegate business I/O and add experimental image masking."""

from __future__ import annotations

import base64
import json
import os
import runpy
import sys
from functools import cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw


@cache
def business_handler():
    root = Path(
        os.environ.get("VISTA_BUSINESS_ROOT")
        or Path(__file__).resolve().parents[2] / "dsh-shader-11-13"
    )
    return runpy.run_path(str(root / "runtime/vista_bridge.py"))["handle"]


def handle(request: dict) -> dict:
    action = request["action"]
    if action == "mask":
        data = base64.b64decode(request["data"], validate=True)
        with Image.open(BytesIO(data)) as image:
            alpha = "A" in image.getbands() or "transparency" in image.info
            image = image.convert("RGBA" if alpha else "RGB")
            draw = ImageDraw.Draw(image)
            for region in request["regions"]:
                x, y, width, height = (
                    region[key] for key in ("x", "y", "width", "height")
                )
                if (
                    any(type(v) is not int for v in (x, y, width, height))
                    or x < 0
                    or y < 0
                    or width < 1
                    or height < 1
                    or x + width > image.width
                    or y + height > image.height
                ):
                    raise ValueError("Mask rectangle is outside the image")
                draw.rectangle(
                    (x, y, x + width - 1, y + height - 1),
                    fill=(32, 36, 43, 255) if alpha else (32, 36, 43),
                )
            output = BytesIO()
            image.save(output, format="PNG")
        return {
            "data": base64.b64encode(output.getvalue()).decode(),
            "media_type": "image/png",
        }
    return business_handler()(request)


def main() -> None:
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            response = {"id": request["id"], "result": handle(request)}
        except Exception as error:
            response = {
                "id": request.get("id") if isinstance(request, dict) else None,
                "error": {"type": type(error).__name__, "message": str(error)},
            }
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
