import json

from scripts.shader_recall_score import evidence_coverage, score_trace


def event(seq, time, name, args):
    return {
        "seq": seq,
        "time": time,
        "type": "tool/call",
        "data": {
            "callId": f"call-{seq}",
            "name": name,
            "arguments": args,
        },
    }


def request(number, time, record=None):
    return {
        "requestId": number,
        "time": time,
        "model": "deepseek-flash",
        "images": [{"source": {"record": record}, "sha256": "pixels"}]
        if record
        else [],
    }


def truth():
    return {
        "acceptable_evidence_sets": [[{"record": "old.json", "content": ["image"]}]],
        "decision_annotations": [{"event_seq": 4, "classification": "target"}],
    }


def test_preparation_edit_does_not_make_recall_late():
    events = [
        event(1, 1000, "edit", {}),
        event(2, 2000, "mcp__vista__read_record", {"record": "old.json"}),
        event(4, 4000, "edit", {}),
    ]
    annotation = truth()
    annotation["decision_annotations"].append(
        {"event_seq": 1, "classification": "preparation"}
    )
    result = score_trace(
        events, [request(1, 1500), request(2, 3000, "old.json")], annotation
    )
    assert result["first_target_decision_seq"] == 4
    assert result["recall_triggered_before_decision"] is True
    assert result["timely_related_evidence"] is True


def test_later_image_does_not_prove_visibility_at_earlier_decision():
    events = [
        event(2, 2000, "mcp__vista__read_record", {"record": "old.json"}),
        event(4, 4000, "edit", {}),
    ]
    result = score_trace(
        events, [request(1, 3000), request(2, 5000, "old.json")], truth()
    )
    assert result["timely_related_evidence"] is False
    assert result["requests_with_required_evidence"] == [2]


def test_same_response_parallel_read_and_edit_is_not_prior_evidence():
    events = [
        event(2, 4000, "mcp__vista__read_record", {"record": "old.json"}),
        event(4, 4001, "edit", {}),
    ]
    result = score_trace(
        events, [request(1, 3000), request(2, 5000, "old.json")], truth()
    )
    assert result["recall_triggered_before_decision"] is False
    assert result["timely_related_evidence"] is False


def test_unclassified_edit_keeps_timing_unknown():
    events = [event(1, 1000, "edit", {}), event(4, 4000, "edit", {})]
    result = score_trace(events, [request(1, 3000, "old.json")], truth())
    assert result["status"] == "timing-unverified"
    assert result["unclassified_prior_actions"] == [1]
    assert result["timely_related_evidence"] is None


def test_explicit_unknown_annotation_keeps_timing_unknown():
    events = [event(1, 1000, "edit", {}), event(4, 4000, "edit", {})]
    annotation = truth()
    annotation["decision_annotations"].append(
        {"event_seq": 1, "classification": "unknown"}
    )
    result = score_trace(events, [request(1, 3000, "old.json")], annotation)
    assert result["status"] == "timing-unverified"
    assert result["unclassified_prior_actions"] == [1]


def test_image_code_and_parameters_each_require_their_own_visibility():
    annotation = {
        "records": {"r.json": {"code_sha256": "code", "metadata_sha256": "meta"}},
        "acceptable_evidence_sets": [
            [{"record": "r.json", "content": ["image", "code", "parameters"]}]
        ],
    }
    row = request(1, 1000, "r.json")
    assert evidence_coverage(row, annotation) is False
    row["includedCodeSha256"] = ["code"]
    assert evidence_coverage(row, annotation) is False
    row["includedMetadataSha256"] = ["meta"]
    assert evidence_coverage(row, annotation) is True


def test_invalid_vista_call_is_counted_without_triggering_recall():
    events = [
        event(2, 2000, "mcp__vista__read_record", "{broken"),
        event(4, 4000, "edit", {}),
    ]
    result = score_trace(events, [request(1, 3000)], truth())
    assert result["vista_read_calls"] == 1
    assert result["vista_reads"] == 0
    assert result["invalid_argument_events"] == [2]
    assert result["recall_triggered_before_decision"] is False
    assert result["timely_related_evidence"] is False


def test_reading_current_reference_or_visible_history_is_not_active_recall():
    expected = truth() | {
        "current_record": "current.json",
        "reference_record": "ref.json",
        "records": {"old.json": {}, "current.json": {}},
        "path_records": {"/current.png": "current.json"},
    }
    events = [
        event(i, 2000, "mcp__vista__read_record", {"record": record})
        for i, record in enumerate(["ref.json", "current.json", "old.json", "missing"])
    ]
    events.append(event(5, 3000, "read_image", {"file_path": "/current.png"}))
    result = score_trace(events, [request(1, 1500, "old.json")], expected)
    assert result["vista_read_calls"] == 4
    assert not result["recall_attempted_in_run"]
    assert [r["classification"] for r in result["record_reads"]] == [
        "current-or-reference",
        "current-or-reference",
        "already-visible",
        "invalid",
        "current-or-reference",
    ]


def test_previous_current_can_be_recalled_after_new_render():
    expected = truth() | {"current_record": "a", "records": {"a": {}}}
    events = [
        event(1, 1000, "render_shader", {}),
        {
            "type": "tool/result",
            "seq": 2,
            "time": 1500,
            "data": {
                "message": {
                    "toolCallId": "call-1",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "vista": {"record": "b"},
                                    "imagePath": "/b.png",
                                    "id": "candidate-b",
                                }
                            ),
                        }
                    ],
                }
            },
        },
        event(3, 2200, "mcp__vista__read_record", {"record": "a"}),
        event(4, 2400, "read_image", {"file_path": "/b.png"}),
    ]
    result = score_trace(events, [request(1, 2000)], expected)
    assert [r["record"] for r in result["recalls"]] == ["a"]


def test_visible_image_does_not_preclude_requesting_old_source():
    expected = truth() | {
        "records": {
            "old.json": {
                "code_sha256": "code",
                "metadata_sha256": "meta",
            }
        }
    }
    event1 = event(
        1, 2000, "mcp__vista__read_record", {"record": "old.json", "content": "both"}
    )
    seen = request(1, 1500, "old.json")
    assert score_trace([event1], [seen], expected)["vista_reads"] == 1
    seen.update(includedCodeSha256=["code"], includedMetadataSha256=["meta"])
    assert score_trace([event1], [seen], expected)["vista_reads"] == 0
