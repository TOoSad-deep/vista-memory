import base64
import hashlib
import json
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from scripts.shader_recall_bridge import handle


def png_bytes():
    image = Image.new("RGB", (20, 10), (220, 40, 70))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_mask_preserves_common_pixels_and_original_input():
    data = png_bytes()
    result = handle(
        {
            "action": "mask",
            "data": base64.b64encode(data).decode(),
            "regions": [{"x": 10, "y": 0, "width": 10, "height": 10}],
        }
    )
    with Image.open(BytesIO(base64.b64decode(result["data"]))) as image:
        assert image.size == (20, 10)
        assert image.getpixel((9, 5)) == (220, 40, 70)
        assert image.getpixel((10, 5)) == (32, 36, 43)
        assert image.getpixel((19, 9)) == (32, 36, 43)
    assert data == png_bytes()


def test_mask_preserves_alpha_in_common_regions():
    output = BytesIO()
    Image.new("RGBA", (2, 1), (220, 40, 70, 80)).save(output, format="PNG")
    result = handle(
        {
            "action": "mask",
            "data": base64.b64encode(output.getvalue()).decode(),
            "regions": [{"x": 1, "y": 0, "width": 1, "height": 1}],
        }
    )
    with Image.open(BytesIO(base64.b64decode(result["data"]))) as image:
        assert image.getpixel((0, 0)) == (220, 40, 70, 80)
        assert image.getpixel((1, 0)) == (32, 36, 43, 255)


@pytest.mark.parametrize(
    "region",
    [
        {"x": -1, "y": 0, "width": 1, "height": 1},
        {"x": 10, "y": 0, "width": 11, "height": 1},
        {"x": 0, "y": 0, "width": 1.5, "height": 1},
    ],
)
def test_invalid_mask_does_not_produce_misleading_control_image(region):
    with pytest.raises(ValueError, match="outside"):
        handle(
            {
                "action": "mask",
                "data": base64.b64encode(png_bytes()).decode(),
                "regions": [region],
            }
        )


def test_bridge_reports_bad_json_then_accepts_next_request(tmp_path):
    script = Path(__file__).parents[1] / "scripts/shader_recall_bridge.py"
    request = {"id": 2, "action": "list", "root": str(tmp_path / "vista")}
    result = subprocess.run(
        [sys.executable, str(script)],
        input="{bad\n" + json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        check=True,
    )
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert responses[0]["id"] is None
    assert responses[0]["error"]["type"] == "JSONDecodeError"
    assert responses[1] == {"id": 2, "result": {"records": []}}


def test_save_bridge_preserves_source_line_endings(tmp_path):
    image = tmp_path / "render.png"
    image.write_bytes(png_bytes())
    shader = tmp_path / "shader.frag"
    original = b"#version 300 es\r\nvoid main() {}\r\n"
    shader.write_bytes(original)
    result = handle(
        {
            "action": "save",
            "root": str(tmp_path / "vista"),
            "image_path": str(image),
            "source_path": str(shader),
            "label": "CRLF",
        }
    )
    metadata = json.loads((tmp_path / "vista/json" / result["record"]).read_text())
    assert (tmp_path / "vista" / metadata["glsl"]).read_bytes() == original


@pytest.fixture
def recovery_requests(tmp_path):
    png = png_bytes()
    code = b"void main() {}\r\n"
    image = tmp_path / "render.png"
    shader = tmp_path / "shader.frag"
    image.write_bytes(png)
    shader.write_bytes(code)
    save = {
        "action": "save",
        "root": str(tmp_path / "vista"),
        "image_path": str(image),
        "source_path": str(shader),
        "label": "candidate-1-render",
        "render": {"time": 0, "uniforms": {"shine": 0.625}},
        "source_agent": None,
    }
    identity = {
        "candidate_id": "original-candidate",
        "image_hash": hashlib.sha256(png).hexdigest(),
        "source_hash": hashlib.sha256(code).hexdigest(),
    }
    recover = {
        **save,
        **identity,
        "source_agent": "shader-author",
        "mapping_path": str(tmp_path / "vista-records.json"),
        "unique_candidate": True,
    }
    return save, identity, recover


@pytest.mark.parametrize("keyed", [False, True], ids=["legacy-uuid4", "keyed-uuid5"])
def test_recovery_preserves_original_source_agent(recovery_requests, keyed):
    save, identity, recover = recovery_requests
    original = handle({**save, **identity} if keyed else save)
    assert handle(recover) == original
    assert original["source_agent"] is None
    assert handle({"action": "list", "root": save["root"]})["records"] == [original]


def test_recovery_does_not_use_default_agent_to_break_legacy_tie(recovery_requests):
    save, _, recover = recovery_requests
    originals = [handle(save), handle({**save, "source_agent": "shader-author"})]
    with pytest.raises(ValueError, match="Ambiguous legacy record"):
        handle(recover)
    records = handle({"action": "list", "root": save["root"]})["records"]
    assert records == sorted(originals, key=lambda row: row["record"])
