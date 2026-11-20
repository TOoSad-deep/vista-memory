"""Exercise the experiment CLI's retry and DSH configuration without model calls."""

from __future__ import annotations

import hashlib
import json
import sys
from importlib import import_module
from pathlib import Path

import pytest


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return import_module("run_vista_history_experiment")


@pytest.mark.parametrize("failed_report", [False, True])
def test_retry_archives_incomplete_case_and_reuses_completed_case(
    tmp_path, monkeypatch, capsys, runner, failed_report
):
    identifier = "set-1-visual-A"
    tokenizer = tmp_path / "tokenizer.json"
    tokenizer.write_text("{}")
    manifest = {
        "sets": [{"task": "set-1", "cases": [{"id": "visual"}]}],
        "tokenizer_sha256": hashlib.sha256(tokenizer.read_bytes()).hexdigest(),
    }
    (tmp_path / "frozen.json").write_text(json.dumps(manifest))
    folder = tmp_path / "runs" / identifier
    folder.mkdir(parents=True)
    (folder / "wire.jsonl").write_text("partial evidence\n")

    def report(reason):
        return {
            "id": identifier,
            "finished": "2026-11-20T00:00:00+08:00",
            "events": [{"type": "turn/end", "data": {"reason": {"kind": reason}}}],
        }

    if failed_report:
        (folder / "run.json").write_text(json.dumps(report("error")))
    calls = []

    def run_case(args, condition):
        assert condition[0] == identifier
        calls.append(condition[0])
        folder.mkdir()
        (folder / "run.json").write_text(json.dumps(report("completed")))
        return True

    argv = ["run", str(tmp_path), "--tokenizer", str(tokenizer), "--only", identifier]
    monkeypatch.setattr(runner, "run_case", run_case)
    monkeypatch.setattr(sys, "argv", argv)
    assert runner.main() == 1
    assert "--retry-failed" in capsys.readouterr().err
    assert not calls

    monkeypatch.setattr(sys, "argv", [*argv, "--retry-failed"])
    assert runner.main() == 0
    (archive,) = (tmp_path / "retry-evidence").iterdir()
    assert (archive / "wire.jsonl").read_text() == "partial evidence\n"
    assert (archive / "run.json").exists() == failed_report

    monkeypatch.setattr(sys, "argv", argv)
    assert runner.main() == 0
    assert calls == [identifier]


def test_overlay_uses_current_dsh_home_or_explicit_credentials(
    tmp_path, monkeypatch, runner
):
    home = tmp_path / "my-dsh"
    explicit = tmp_path / "account.yaml"
    monkeypatch.setenv("DSH_HOME", str(home))

    def credential_path(patches):
        return next(
            entry["config"]["path"]
            for entry in patches[-1]["insert"]
            if entry["id"] == "credentials-local"
        )

    assert credential_path(runner.overlay({}, {})) == str(home / ".credentials.yaml")
    assert credential_path(runner.overlay({}, {}, explicit)) == str(explicit)

    monkeypatch.delenv("DSH_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert credential_path(runner.overlay({}, {})) == str(
        tmp_path / ".dsh" / ".credentials.yaml"
    )
