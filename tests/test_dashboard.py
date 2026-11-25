"""Checks that the dashboard runs the same experiment as the command line."""

import json
import math

import pandas as pd
import pytest

import dashboard
import experiment
import simulator


SMALL = {
    "team_sizes": [3], "comm_ranges": ["4"], "trials": 1, "trial_index": 2, "master_seed": 7,
    "obstacle_density": 0.2, "open_cells_per_robot": 40, "replan": "immediate",
    "sensing_radius": 5, "step_cap_factor": 4,
}


def test_live_trial_matches_sweep_trial(tmp_path):
    """A live trial with (seed, N, r_c, trial) reproduces the sweep's row for the same identity."""
    config = dashboard.validate_config(SMALL, "trial")
    (tmp_path / "config.json").write_text(json.dumps(config))
    dashboard.run_live_trial(tmp_path, pace=False)
    live = pd.read_csv(tmp_path / "trials.csv").iloc[0]
    sweep = simulator.run_trial(n_robots=3, r_c=4.0, trial_index=2, master_seed=7,
                                open_cells_per_robot=40)["summary"]
    for key in ("steps", "W", "final_coverage", "stop_reason", "start_cells"):
        assert live[key] == sweep[key]
    frames = [json.loads(line) for line in (tmp_path / "frames.jsonl").read_text().splitlines()]
    assert frames[0]["cap"] == 4 * frames[0]["open"]
    assert frames[-1]["wasted"] == sweep["W"]
    assert frames[-1]["t"] == sweep["steps"]
    steps = pd.read_csv(tmp_path / "steps.csv.gz")
    assert list(steps.columns[:3]) == ["N", "r_c", "trial"]


def test_single_robot_frames_are_json_safe(tmp_path):
    config = dashboard.validate_config({**SMALL, "team_sizes": [1]}, "trial")
    (tmp_path / "config.json").write_text(json.dumps(config))
    dashboard.run_live_trial(tmp_path, pace=False)
    for line in (tmp_path / "frames.jsonl").read_text().splitlines():
        json.loads(line, parse_constant=lambda c: pytest.fail(f"non-JSON constant {c}"))


def test_validate_config_rejects_bad_input():
    with pytest.raises(ValueError):
        dashboard.validate_config({**SMALL, "team_sizes": [3, 4]}, "trial")
    with pytest.raises(ValueError):
        dashboard.validate_config({**SMALL, "comm_ranges": ["-1"]}, "sweep")
    with pytest.raises(ValueError):
        dashboard.validate_config({**SMALL, "comm_ranges": ["far"]}, "sweep")
    sweep = dashboard.validate_config({**SMALL, "comm_ranges": ["unlimited", "0"]}, "sweep")
    assert sweep["comm_ranges"] == ["0", "unlimited"]
    assert "trial_index" not in sweep


def test_reproduction_check():
    expected = {"steps": "96", "W": "1002", "stop_reason": "step_cap", "F": "96",
                "start_cells": "1,0;2,0", "final_coverage": "0.99", "t100": ""}
    rendered = {"steps": 96, "W": 1002, "stop_reason": "step_cap", "F": 96,
                "start_cells": "1,0;2,0", "final_coverage": .99, "t100": None}
    assert dashboard.reproduction_check(rendered, expected)["match"]
    for field, value in (("W", 1001), ("start_cells", "3,0;4,0"), ("t100", 96)):
        check = dashboard.reproduction_check({**rendered, field: value}, expected)
        assert not check["match"] and check["mismatches"] == [field]
    assert not dashboard.reproduction_check({}, expected)["match"]


def test_clean_replaces_nan():
    assert dashboard.clean({"a": [math.nan, 1.0, math.inf]}) == {"a": [None, 1.0, None]}


def test_sweep_with_every_trial_failing_still_writes_manifest(tmp_path, monkeypatch):
    config = tmp_path / "bad.json"
    config.write_text(json.dumps({"team_sizes": [2], "comm_ranges": ["unlimited"], "trials": 1, "replan": "immediate",
                                  "open_cells_per_robot": -5}))
    with pytest.raises(SystemExit):
        experiment.main([str(config), "--out", str(tmp_path / "out"), "--workers", "1"])
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert manifest["trials_failed"] == 1 and manifest["trials_run"] == 0
    monkeypatch.setattr(dashboard, "RESULTS", tmp_path)
    monkeypatch.setattr(dashboard, "RUNS", tmp_path / "dashboard")
    assert dashboard.list_results()[0]["status"] == "failed"
    detail = dashboard.run_details(tmp_path / "out")
    assert detail["failures"][0]["r_c"] == "unlimited"
    assert detail["settings"][0]["trials"] == 0


def test_setting_table_retains_settings_without_results():
    cfg = {"team_sizes": [2], "comm_ranges": [0, "unlimited"], "trials": 2}
    rows = [
        {"N": "2", "r_c": "0", "stop_reason": "step_cap", "W_per_F": "0.2", "t90": ""},
        {"N": "2", "r_c": "0", "stop_reason": "full_coverage", "W_per_F": "0.4", "t90": "20"},
    ]
    finite, unlimited = dashboard.clean(dashboard.setting_table(rows, cfg))
    assert (finite["completed"], finite["trials"], finite["reached_90"], finite["t90_median"]) == (1, 2, 1, 20)
    assert unlimited["trials"] == 0 and unlimited["expected"] == 2 and unlimited["W_per_F_median"] is None
