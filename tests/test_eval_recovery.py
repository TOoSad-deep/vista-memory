"""Regression checks for interrupted experiments and archived inputs."""

import fcntl
import importlib
import json
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def scripts(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module


def test_resume_interrupted_pair_without_overwriting_original(
    tmp_path, monkeypatch, scripts
):
    runner = scripts("run_vista_memory_eval")
    args = Namespace(output=tmp_path, phase="formal", attempt=1)
    pair = {"case": {"id": "R1-01"}, "repeat": 1, "conditions": ["text", "visual"]}
    runs = tmp_path / "formal/runs"
    first = runs / "R1-01-r1-text-a1"
    first.mkdir(parents=True)
    report = first / "run.json"
    report.write_text('{"status":"running"}')
    run = Mock(return_value={"status": "completed"})
    monkeypatch.setattr(runner, "run_one", run)
    monkeypatch.setattr(
        runner.subprocess,
        "check_output",
        lambda *a, **k: f"python /x/dsh_sdk_run.py --output {report}",
    )
    assert runner.run_pair(pair, args)[0]["status"] == "running"
    run.assert_not_called()
    monkeypatch.setattr(runner.subprocess, "check_output", lambda *a, **k: "")
    runner.run_pair(pair, args)
    assert [(call.args[1], call.args[3].attempt) for call in run.call_args_list] == [
        ("text", 2),
        ("visual", 2),
    ]
    assert report.read_text() == '{"status":"running"}'
    with (runs / ".R1-01-r1.lock").open("a") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        run.reset_mock()
        assert runner.run_pair(pair, args)[0]["status"] == "running"
        run.assert_not_called()


@pytest.mark.parametrize("probe", [None, "{", '{"status":"running"}'])
def test_missing_terminal_probe_stops_batch(tmp_path, monkeypatch, scripts, probe):
    batch = scripts("run_shader_recall_batch")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "T01",
                        "purpose": "opportunity",
                        "checkpoint": "unused",
                        "provided_records": ["old.json"],
                    }
                ]
            }
        )
    )

    def driver(command, **kwargs):
        run = Path(command[command.index("--output") + 1])
        run.mkdir(parents=True)
        if probe is not None:
            (run / "probe.json").write_text(probe)
        return Namespace(returncode=1)

    monkeypatch.setattr(batch.subprocess, "run", driver)
    monkeypatch.setattr(batch, "source_manifest", lambda: {})
    output = tmp_path / "batch"
    status = batch.execute(
        Namespace(manifest=manifest, output=output, phase="diagnostic", seed=1, limit=2)
    )
    result = json.loads((output / "execution.json").read_text())
    assert status != 0 and result["status"] == "needs-repair"
    assert [row["status"] for row in result["runs"]] == ["driver-failed"]


def test_failed_shutdown_forces_cleanup_and_preserves_cause(
    tmp_path, monkeypatch, scripts
):
    driver = scripts("run_shader_recall")
    rpc = Mock()
    rpc.process.pid = 101
    rpc.process.poll.return_value = None
    rpc.stderr, rpc.notifications, rpc.responses = [], [], []

    def request(method, *args):
        raise TimeoutError(method)

    rpc.request.side_effect = request
    monkeypatch.setattr(driver, "RpcProcess", lambda *args: rpc)
    output = tmp_path / "run"
    monkeypatch.setattr(sys, "argv", ["probe", "probe", "--output", str(output)])
    assert driver.main() == 1
    rpc.close.assert_called_once()
    report = json.loads((output / "probe.json").read_text())
    assert report["error"]["message"] == "initialize"
    assert report["cleanup_error"]["message"] == "shutdown"


def test_relocated_legacy_repair_uses_local_parent_dataset(tmp_path, scripts):
    paths = scripts("vista_eval_paths")
    folder = tmp_path / "dataset-v3"
    folder.mkdir()
    case = {"id": "C", **{key: f"/old/dataset-v3/C/{key}" for key in paths.CASE_PATHS}}
    case["root"] = "/old/dataset-v2/C/vista"
    manifest = folder / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "encoding_repair": {"parent_manifest": "/old/dataset-v2/manifest.json"},
                "cases": [case],
            }
        )
    )
    loaded = paths.load_dataset(manifest)["cases"][0]
    assert Path(loaded["root"]) == tmp_path / "dataset-v2/C/vista"
    assert Path(loaded["target_path"]) == folder / "C/target_path"
    assert paths.relative_case(loaded, folder)["root"] == "../dataset-v2/C/vista"
