"""Check the two archived cases offline. All indexes are local to one scored question."""

import hashlib
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1]
frozen = json.loads((OUT / "frozen.json").read_text())
target = frozen["target"]
digest = hashlib.sha256((OUT / "catalog" / target["png"]).read_bytes()).hexdigest()
assert digest == target["sha256"]
summary = {}
schemas = []
for case in ("a", "b"):
    folder = OUT / case
    run = json.loads((folder / "run.json").read_text())
    wire = [
        json.loads(line) for line in (folder / "wire.jsonl").read_text().splitlines()
    ]
    assert any(
        {"id": "deepseek-flash", "name": "DeepSeek-V4.1-Flash"}
        in row.get("matches", [])
        and row["status"] == 200
        for row in wire
    )
    uploads = {
        r["file"]: r["sha256"]
        for r in wire
        if r["kind"] == "upload" and r["status"] == 200
    }
    requests = [r for r in wire if r["kind"] == "request"]
    statuses = {
        (r["phase"], r["n"]): r["status"] for r in wire if r["kind"] == "response"
    }
    assert all(statuses[r["phase"], r["n"]] == 200 for r in requests)
    assert all(
        r["model"] == frozen["model"] and r["tools"] == frozen["allowed_tools"]
        for r in requests
    )
    history = [r for r in requests if r["phase"] == "history"]
    assert len(history) == 1 and [uploads[i["file"]] for i in history[0]["images"]] == [
        digest
    ]
    questions = [r for r in requests if r["phase"] == "question"]
    first = questions[0]
    schemas.append(first["body"]["tools"])
    assert (
        not first["images"]
        and first["body"]["system"] == "You are a helpful software engineer assistant."
    )
    assert len(first["body"]["messages"]) == 1
    text = first["body"]["messages"][0]["content"]
    assert [x["text"] for x in text[:2]] == [
        frozen["summary"],
        frozen["questions"][case],
    ]
    assert len(text) == 3 and "Current working directory" in text[-1]["text"]
    assert not any(
        record["record"] in json.dumps(first["body"]) for record in frozen["records"]
    )
    turns = [
        t
        for t in run["turns"]
        if t["stage"] == "question"
        and t["events"][-1]["data"]["reason"]["kind"] == "completed"
    ]
    assert len(turns) == 1
    events = turns[0]["events"]
    tools = []
    results = {}
    answer = None
    for e in events:
        if e["type"] == "assistant/message":
            for block in e["data"]["message"]["content"]:
                if block["type"] == "text":
                    answer = block["text"]
                if block["type"] == "tool-call":
                    args = block["arguments"]
                    tools.append(
                        dict(
                            seq=e["seq"],
                            call=block["id"],
                            tool=block["name"],
                            args=json.loads(args) if isinstance(args, str) else args,
                        )
                    )
        if e["type"] == "tool/result":
            msg = e["data"]["message"]
            assert not msg["isError"]
            results[msg["toolCallId"]] = (e["seq"], msg["content"])
    if case == "a":
        assert [t["tool"] for t in tools] == frozen["allowed_tools"]
        listing, read = tools
        lseq, blocks = results[listing["call"]]
        catalog = json.loads(blocks[0]["text"])["records"]
        assert {v["record"] for v in catalog} == {
            v["record"] for v in frozen["records"]
        }
        assert read["args"]["record"] == target["record"] and read["args"].get(
            "content", "image"
        ) in ("image", "both")
        rseq, blocks = results[read["call"]]
        assert (
            json.loads(blocks[0]["text"].splitlines()[0])["record"] == target["record"]
        )
        assert [
            b["attachment"]["attachmentId"] for b in blocks if b["type"] == "image"
        ] == ["sha256:" + digest]
        assert len(questions) == 3 and [len(r["images"]) for r in questions] == [
            0,
            0,
            1,
        ]
        im = questions[2]["images"][0]
        assert (
            im["call"] == read["call"] and uploads[im["file"]] == im["sha256"] == digest
        )
        assert "下方" in answer
    else:
        assert (
            not tools
            and not results
            and len(questions) == 1
            and not questions[0]["images"]
        )
        assert "蓝色" in answer
    summary[case] = dict(
        session=run["session"],
        question=frozen["questions"][case],
        answer=answer,
        tools=tools,
        previous_image_sha256=digest,
        first_user_and_history=[x["text"] for x in text[:2]],
        requests=[
            dict(n=r["n"], status=statuses[r["phase"], r["n"]], images=r["images"])
            for r in questions
        ],
        trace=dict(directory_result=lseq, summaries=len(catalog), read_result=rseq)
        if case == "a"
        else dict(summaries=0),
        history_read_pass=True,
        answer_matches=True,
    )
assert schemas[0] == schemas[1] and summary["a"]["session"] != summary["b"]["session"]
(OUT / "outcome.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
)
print("2 independent cases verified")
