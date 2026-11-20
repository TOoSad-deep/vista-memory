"""Verify tool provenance and causal order in the focused recall gate."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from shader_recall_followup import READ, wire_chain  # noqa: E402
from shader_trigger_validation import digest, verified_candidate_parameters  # noqa: E402


def test_only_tool_sourced_material_after_valid_read_before_edit_counts():
    s = {"record": "old", "imageHash": "png", "sourceHash": "glsl"}
    listings = [{"success": True, "records": ["old"], "result_seq": 3, "call_step": 1}]
    reads = [
        {
            "success": True,
            "record": "old",
            "result_seq": 7,
            "result_time": 70,
            "call_seq": 6,
            "call_step": 2,
            "call_id": "read",
            "png": True,
            "glsl": True,
        }
    ]
    image = {
        "sha256": "png",
        "mimeType": "image/png",
        "location": "image",
        "toolCallId": "read",
        "source": {"record": "old", "channel": READ},
    }
    req = {
        "requestId": 3,
        "time": 80,
        "images": [image],
        "includedCodeSha256": ["glsl"],
        "codeSources": [{"toolCallId": "read", "sha256": "glsl"}],
    }

    def check(request=req, results=reads, cutoff=90, status=200):
        chain = wire_chain(
            s, listings, results, [request], {3: status}, {"time": cutoff}, [], "meta"
        )
        return chain["qualifying_requests"]

    assert check() == [3]
    assert not check({**req, "time": 60})
    assert not check(cutoff=75)
    assert not check(results=[{**reads[0], "call_step": 1}])
    assert not check(results=[{**reads[0], "success": False}])
    assert not check({**req, "images": [{**image, "toolCallId": "file-read"}]})
    assert not check({**req, "codeSources": [{"sha256": "glsl"}]})
    assert not check(status=403)
    no_edit = wire_chain(s, listings, reads, [req], {3: 200}, None, [], "meta")
    assert no_edit["wire_visibility"][0]["proven_read_glsl"] == ["read"]
    assert not no_edit["qualifying_requests"]


def test_mixed_shell_output_proves_parameters_only_with_same_wire_text_and_version():
    text = (
        'old/\n{"id":"old","imageHash":"png","sourceHash":"src",'
        '"width":32,"height":20,"time":0,"params":[{"name":"shine","default":1}]}'
        "\n----- shader\nvoid main(){}\n"
    )
    message = {"content": [{"type": "text", "text": text}]}
    target = {"candidateId": "old", "imageHash": "png", "sourceHash": "src"}
    render = {"width": 32, "height": 20, "time": 0, "uniforms": {"shine": 1}}
    checksum = digest(text.encode())
    assert verified_candidate_parameters(message, checksum, target, render)
    assert not verified_candidate_parameters(message, digest(b"pruned"), target, render)
    assert not verified_candidate_parameters(
        message, checksum, {**target, "candidateId": "new"}, render
    )
    assert not verified_candidate_parameters(
        message, checksum, target, {**render, "uniforms": {"shine": 0.2}}
    )
