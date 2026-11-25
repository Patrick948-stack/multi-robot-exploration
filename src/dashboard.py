"""Serve the local experiment dashboard, or execute its live-trial worker.

Usage:
    python src/dashboard.py            # then open http://127.0.0.1:8765
    python src/dashboard.py --port 8800

Every run goes through the same code as the command line: live trials call
simulator.run_trial, sweeps call experiment.py, figures call analysis.py and
videos call animate.py. Dashboard runs are saved under results/dashboard/.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import product
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import numpy as np
import pandas as pd

import experiment
import simulator

ROOT = experiment.ROOT
RESULTS = ROOT / "results"
RUNS = RESULTS / "dashboard"
UI = Path(__file__).with_name("dashboard_ui")
LOCAL = "127.0.0.1"
PLAN_DEFAULTS = {**experiment.DEFAULTS, "team_sizes": [4], "comm_ranges": [6], "trials": 20, "trial_index": 0}
DEFAULT_WORKERS = min(4, os.cpu_count() or 1)
VIDEO_FORMAT = "mp4" if shutil.which("ffmpeg") else "gif"
LIVE_STEPS_PER_SECOND = 20
DATA_FILES = {
    "config.json": "Settings", "manifest.json": "Manifest (seed, versions, failures)",
    "trials.csv": "Trial summaries", "steps.csv.gz": "Step-by-step data",
    "frames.jsonl": "Replay frames", "run.log": "Run log",
}
LOGS = ("run.log", "analysis.log", "video.log")
FIGURES = {
    "wasted_effort_vs_range.png": "Repeated discoveries per open cell against range",
    "coverage_curves.png": "Coverage over time",
    "steps_to_coverage.png": "Steps to 90, 99 and 100% coverage",
    "predictors.png": "Information age and λ₂ against repeated discoveries",
    "summary_by_setting.csv": "Summary by setting",
    "correlations.csv": "Rank correlations",
}
VIDEO_NAME = re.compile(r"N\d+_rc[\w.]+_trial\d+_robot\d+\.(?:gif|mp4)|trial-\d+-robot-\d+\.(?:gif|mp4)")
TRIAL_COLUMNS = ("N", "r_c", "r_c_label", "trial", "steps", "final_coverage", "W", "W_per_F",
                 "t90", "t99", "t100", "stop_reason", "F", "start_cells")


def clean(value):
    """JSON-safe copy: NaN and infinities become None."""
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def validate_config(data, mode):
    def integer(key, value, lower, upper):
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"{key} must be a whole number from {lower:,} to {upper:,}.")
        return value

    sizes = data["team_sizes"]
    ranges = data["comm_ranges"]
    if not isinstance(sizes, list) or not isinstance(ranges, list) or not sizes or not ranges:
        raise ValueError("Enter at least one team size and communication range.")
    if mode == "trial" and (len(sizes) != 1 or len(ranges) != 1):
        raise ValueError("A live trial takes one team size and one communication range.")
    sizes = [integer("Team size", n, 1, 64) for n in sizes]
    try:
        ranges = [experiment.parse_range(r) for r in ranges]
    except ValueError:
        raise ValueError("Communication ranges must be numbers or 'unlimited'.") from None
    if any(math.isnan(r) or r < 0 for r in ranges):
        raise ValueError("Communication ranges must be 0 or more, or 'unlimited'.")
    if len(set(sizes)) != len(sizes) or len(set(ranges)) != len(ranges):
        raise ValueError("Enter each team size and communication range once.")
    trials = integer("Trials per setting", data["trials"], 1, 1000) if mode == "sweep" else 1
    if len(sizes) * len(ranges) * trials > 10_000:
        raise ValueError("A sweep can contain up to 10,000 trials.")
    density = float(data["obstacle_density"])
    if not 0 <= density <= 0.5:
        raise ValueError("Obstacle density must be between 0% and 50%.")
    if data["replan"] not in simulator.REPLAN_RULES:
        raise ValueError("Choose a replanning rule.")
    config = {
        "team_sizes": sorted(sizes),
        "comm_ranges": [experiment.range_label(r) for r in sorted(ranges)],
        "trials": trials,
        "master_seed": integer("Master seed", data["master_seed"], 0, 2**32 - 1),
        "obstacle_density": density,
        "open_cells_per_robot": integer("Open cells per robot", data["open_cells_per_robot"], 25, 2000),
        "replan": data["replan"],
        "sensing_radius": integer("Sensing radius", data["sensing_radius"], 1, 20),
        "step_cap_factor": integer("Step cap factor", data["step_cap_factor"], 1, 20),
    }
    if mode == "trial":
        config["trial_index"] = integer("Trial", data.get("trial_index", 0), 0, 2**32 - 1)
    return config


def run_config(directory):
    """The settings a run used: the manifest's copy if it finished, else config.json."""
    manifest = directory / "manifest.json"
    if manifest.is_file():
        data = json.loads(manifest.read_text())
        return {**experiment.DEFAULTS, **data["config"]}, data
    config = directory / "config.json"
    return ({**experiment.DEFAULTS, **json.loads(config.read_text())} if config.is_file() else None), None


# ---------------------------------------------------------------------------
# Live trial worker
# ---------------------------------------------------------------------------

def trial_frames(config, emit, pace=True):
    """Run one trial through simulator.run_trial and emit a frame per step."""
    interval = 1 / LIVE_STEPS_PER_SECOND
    last = time.monotonic()

    def on_frame(terrain, positions, adjacency, seen, repeated, metrics):
        nonlocal last
        t = metrics["t"]
        if t == 0:
            emit({
                "shape": list(terrain.shape),
                "walls": np.flatnonzero(terrain.grid.ravel() == simulator.WALL).tolist(),
                "entrance": list(terrain.entrance),
                "open": terrain.n_open,
                "cap": config["step_cap_factor"] * terrain.n_open,
                "robots": len(positions),
                "range": config["comm_ranges"][0],
                "trial": config["trial_index"],
                "seed": config["master_seed"],
                "sensing": config["sensing_radius"],
            })
        if pace and t:
            # Pace the run so the workspace can follow individual moves.
            time.sleep(max(0.0, interval - (time.monotonic() - last)))
        last = time.monotonic()
        emit(clean({
            "t": t,
            "positions": positions.tolist(),
            "links": np.argwhere(np.triu(adjacency, k=1)).tolist(),
            "seen": np.flatnonzero(seen == t).tolist(),
            "repeated": np.unique(repeated).tolist(),
            "coverage": metrics["coverage"],
            "wasted": metrics["wasted_effort"],
            "groups": metrics["n_groups"],
            "age": metrics["mean_information_age"],
            "lambda2": metrics["lambda_2"],
        }))

    return simulator.run_trial(
        n_robots=config["team_sizes"][0],
        r_c=experiment.parse_range(config["comm_ranges"][0]),
        trial_index=config["trial_index"],
        master_seed=config["master_seed"],
        obstacle_density=config["obstacle_density"],
        open_cells_per_robot=config["open_cells_per_robot"],
        replan=config["replan"],
        sensing_radius=config["sensing_radius"],
        step_cap_factor=config["step_cap_factor"],
        on_frame=on_frame,
    )


def run_live_trial(out, pace=True):
    """Write the same files as experiment.py for a one-trial run, plus replay frames."""
    started = datetime.now().astimezone()
    config = json.loads((out / "config.json").read_text())
    with (out / "frames.jsonl").open("w", buffering=1) as stream:
        result = trial_frames(config, lambda frame: stream.write(json.dumps(frame) + "\n"), pace)
    summary = result["summary"]
    identity = {"N": summary["N"], "r_c": summary["r_c"], "trial": summary["trial"]}
    steps = pd.DataFrame(result["steps"])
    for i, (key, value) in enumerate(identity.items()):
        steps.insert(i, key, value)
    steps.to_csv(out / "steps.csv.gz", index=False, compression="gzip")
    row = pd.DataFrame([summary])
    row.insert(2, "r_c_label", experiment.range_label(summary["r_c"]))
    row.to_csv(out / "trials.csv", index=False)
    finished = datetime.now().astimezone()
    manifest = {
        "config": config, "master_seed": config["master_seed"],
        "seed_rule": "numpy SeedSequence(master_seed, spawn_key=(N, trial)) -> map, starts, one stream per robot",
        "started": started.isoformat(timespec="seconds"), "finished": finished.isoformat(timespec="seconds"),
        "runtime_seconds": round((finished - started).total_seconds(), 1),
        "trials_run": 1, "trials_failed": 0, "failures": [], "git": experiment.git_state(),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"{summary['stop_reason']} after {summary['steps']} steps, W = {summary['W']}.")


# ---------------------------------------------------------------------------
# Saved runs
# ---------------------------------------------------------------------------

def find_result(key):
    parts = key.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("Unknown run.")
    if len(parts) != 1 and not (len(parts) == 2 and parts[0] == "dashboard"):
        raise ValueError("Unknown run.")
    directory = RESULTS.joinpath(*parts)
    if not directory.is_dir() or directory.resolve().parent not in {RESULTS.resolve(), RUNS.resolve()}:
        raise ValueError("Unknown run.")
    return directory


def figure_directory(directory):
    """Dashboard runs keep figures inside the run; CLI runs use figures/<name> like analysis.py."""
    if directory.parent == RUNS or (directory / "analysis").exists():
        return directory / "analysis"
    return ROOT / "figures" / directory.name


def video_folder(directory):
    return figure_directory(directory) / "videos"


def relative(path):
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


@lru_cache(maxsize=8)
def read_trials(path, modified):
    with path.open(newline="") as stream:
        return [
            {"index": i, **{key: row.get(key, "") for key in TRIAL_COLUMNS}}
            for i, row in enumerate(csv.DictReader(stream))
        ]


def trial_rows(directory):
    path = directory / "trials.csv"
    return read_trials(path, path.stat().st_mtime_ns)


def setting_table(rows, config):
    """Show every planned setting, including settings with zero recorded trials."""
    groups = {}
    for row in rows:
        key = int(row["N"]), experiment.parse_range(row["r_c"])
        groups.setdefault(key, []).append(row)
    planned = product(config["team_sizes"], map(experiment.parse_range, config["comm_ranges"]))
    return [
        setting_summary(n, r_c, groups.get((n, r_c), []), config["trials"])
        for n, r_c in sorted(set(planned) | groups.keys())
    ]


def setting_summary(n, r_c, rows, expected):
    wasted = pd.Series([float(row["W_per_F"]) for row in rows if row["W_per_F"]], dtype=float)
    t90 = pd.Series([float(row["t90"]) for row in rows if row["t90"]], dtype=float).dropna()
    q25, median, q75 = wasted.quantile([.25, .5, .75])
    return {
        "N": n, "r_c_label": experiment.range_label(r_c), "trials": len(rows),
        "expected": expected, "completed": sum(row["stop_reason"] == "full_coverage" for row in rows),
        "W_per_F_median": median, "W_per_F_q25": q25, "W_per_F_q75": q75,
        "t90_median": t90.median() if len(t90) else None, "reached_90": len(t90),
    }


def run_status(directory, manifest):
    if manifest is not None:
        return "failed" if manifest.get("trials_failed") and not manifest.get("trials_run") else "complete"
    job = JOBS.state()
    if job and job["key"] == directory.relative_to(RESULTS).as_posix() and job["action"] in {"trial", "sweep"}:
        return "running" if job["status"] == "stopping" else job["status"]
    return "incomplete"


def describe_run(directory):
    config, manifest = run_config(directory)
    files = [name for name in DATA_FILES if (directory / name).is_file()]
    if config is None:
        return None
    kind = ("trial" if directory.name.startswith("trial-") else "sweep") if directory.parent == RUNS else "command line"
    figures = [name for name in FIGURES if (figure_directory(directory) / name).is_file()]
    videos = video_folder(directory)
    return {
        "key": directory.relative_to(RESULTS).as_posix(),
        "name": directory.name,
        "kind": kind,
        "status": run_status(directory, manifest),
        "path": relative(directory),
        "figure_path": relative(figure_directory(directory)),
        "config": clean({k: config.get(k) for k in (
            "team_sizes", "comm_ranges", "trials", "trial_index", "master_seed", "obstacle_density",
            "open_cells_per_robot", "replan", "sensing_radius", "step_cap_factor", "preset")}),
        "finished": manifest and (manifest.get("finished_utc") or manifest.get("finished")),
        "runtime": manifest and manifest.get("runtime_seconds"),
        "trials_run": manifest and manifest.get("trials_run"),
        "trials_failed": manifest.get("trials_failed", 0) if manifest else 0,
        "git": manifest and manifest.get("git"),
        "files": files,
        "logs": [name for name in LOGS if (directory / name).is_file()],
        "figures": figures,
        "can_analyze": "trials.csv" in files and manifest is not None,
        "has_replay": "frames.jsonl" in files,
        "videos": sorted(p.name for p in videos.glob("*") if VIDEO_NAME.fullmatch(p.name)) if videos.is_dir() else [],
        "updated": directory.stat().st_mtime,
    }


def list_results():
    results = []
    for folder in (RESULTS, RUNS):
        if not folder.exists():
            continue
        for directory in folder.iterdir():
            if directory.is_dir() and directory != RUNS:
                try:
                    run = describe_run(directory)
                except (OSError, ValueError, KeyError):
                    continue
                if run:
                    results.append(run)
    return sorted(results, key=lambda row: row["updated"], reverse=True)


def run_details(directory):
    config, manifest = run_config(directory)
    rows = trial_rows(directory) if (directory / "trials.csv").is_file() else []
    return clean({
        "rows": rows,
        "settings": setting_table(rows, config) if manifest else [],
        "failures": [
            {"n_robots": f["n_robots"], "r_c": experiment.range_label(experiment.parse_range(f["r_c"])),
             "trial_index": f["trial_index"], "error": f["error"].strip().splitlines()[-1]}
            for f in (manifest or {}).get("failures", [])
        ],
    })


def video_name(row, watch):
    return f"N{row['N']}_rc{row['r_c_label'] or row['r_c']}_trial{row['trial']}_robot{watch}.{VIDEO_FORMAT}"


def video_command(directory, data):
    rows = trial_rows(directory)
    index = data["index"]
    if type(index) is not int or not 0 <= index < len(rows):
        raise ValueError("Select a trial to render.")
    row = rows[index]
    watch = data["watch"]
    if type(watch) is not int or not 0 <= watch < int(row["N"]):
        raise ValueError("Choose a robot in the selected trial.")
    config, _ = run_config(directory)
    folder = video_folder(directory)
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / video_name(row, watch)
    temporary = folder / f".render-{uuid4().hex}{final.suffix}"
    command = [
        "animate.py", "--robots", row["N"], "--range", row["r_c"], "--trial", row["trial"],
        "--seed", str(config["master_seed"]), "--density", str(config["obstacle_density"]),
        "--open-per-robot", str(config["open_cells_per_robot"]), "--replan", config["replan"],
        "--sensing-radius", str(config["sensing_radius"]), "--step-cap-factor", str(config["step_cap_factor"]),
        "--watch", str(watch), "--out", str(temporary), "--video-only",
    ]
    expected = {key: row[key] for key in
                ("steps", "W", "stop_reason", "F", "start_cells", "final_coverage", "t90", "t99", "t100")
                if row[key] or key in {"t90", "t99", "t100"}}
    command.append("--report-summary")
    return command, {"temporary": temporary, "final": final, "expected": expected}


def reproduction_check(summary, expected):
    """Compare the saved row with simulation evidence from the animation worker."""
    mismatches = []
    for key, saved in expected.items():
        rendered = summary.get(key)
        if key in {"stop_reason", "start_cells"}:
            matches = rendered == saved
        elif saved in {"", "nan", "NaN"}:
            matches = key in summary and rendered is None
        else:
            matches = rendered is not None and float(rendered) == float(saved)
        if not matches:
            mismatches.append(key)
    detail = ("Saved trial summary matches the rerun." if not mismatches else
              "Rerun differs from the saved trial in " + ", ".join(mismatches) + ".")
    return {"match": not mismatches, "detail": detail, "expected": expected,
            "rendered": summary, "mismatches": mismatches}


# ---------------------------------------------------------------------------
# Background operations (one at a time)
# ---------------------------------------------------------------------------

class JobManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.job = None

    def start(self, action, data):
        with self.lock:
            if self.process is not None:
                raise ValueError("Another operation is running. Stop it or wait for it to finish.")
            video = None
            total = None
            if action in {"analyze", "video"}:
                out = find_result(data["key"])
                if not all((out / name).is_file() for name in ("trials.csv", "manifest.json")):
                    raise ValueError("This run has no saved results yet.")
                if action == "video":
                    command, video = video_command(out, data)
                    log = out / "video.log"
                else:
                    command = ["analysis.py", str(out), "--out", str(figure_directory(out))]
                    log = out / "analysis.log"
            elif action in {"trial", "sweep"}:
                config = validate_config(data, action)
                workers = data.get("workers", DEFAULT_WORKERS)
                if action == "sweep" and (type(workers) is not int or not 1 <= workers <= (os.cpu_count() or 1)):
                    raise ValueError(f"Workers must be between 1 and {os.cpu_count() or 1}.")
                preset = data.get("preset") or ""
                if action == "sweep" and re.fullmatch(r"[\w-]{1,40}", preset):
                    config["preset"] = preset
                tag = f"-{preset}" if config.get("preset") else ""
                out = RUNS / f"{action}{tag}-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
                out.mkdir(parents=True)
                config["name"] = out.name
                path = out / "config.json"
                path.write_text(json.dumps(config, indent=2) + "\n")
                log = out / "run.log"
                command = (["dashboard.py", "--trial-worker", str(out)] if action == "trial" else
                           ["experiment.py", str(path), "--out", str(out), "--workers", str(workers)])
                total = len(config["team_sizes"]) * len(config["comm_ranges"]) * config["trials"]
            else:
                raise ValueError("Unknown operation.")

            self.job = {
                "action": action, "key": out.relative_to(RESULTS).as_posix(),
                "status": "running", "done": 0, "total": total, "message": "",
                "started": time.time(), "video": video["final"].name if video else None,
                "index": data.get("index"), "watch": data.get("watch"), "check": None,
            }
            try:
                self.process = subprocess.Popen(
                    [sys.executable, "-u", *command], cwd=Path(__file__).resolve().parent,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    start_new_session=True,
                )
            except OSError:
                self.job = None
                raise
            threading.Thread(target=self.monitor, args=(self.process, log, video), daemon=True).start()
            return dict(self.job)

    def monitor(self, process, log, video):
        last_line = ""
        summary = {}
        try:
            with process.stdout as stream, log.open("w", buffering=1) as output:
                for line in stream:
                    output.write(line)
                    last_line = line.strip() or last_line
                    if video and line.startswith("Trial summary: "):
                        summary = json.loads(line.removeprefix("Trial summary: "))
                    progress = re.search(r"(\d+)/(\d+) done", line)
                    if progress:
                        with self.lock:
                            self.job["done"] = int(progress[1])
            code = process.wait()
        except Exception:
            traceback.print_exc()
            self.signal(process, signal.SIGTERM)
            process.wait()
            code = 1
            last_line = "Could not record the operation log."
        with self.lock:
            stopping = self.job["status"] == "stopping"
            if video:
                try:
                    if code == 0 and not stopping:
                        check = reproduction_check(summary, video["expected"])
                        self.job["check"] = check
                        if check["match"]:
                            video["temporary"].replace(video["final"])
                            video["final"].with_suffix(".json").write_text(json.dumps(check, indent=2))
                        else:
                            video["temporary"].unlink(missing_ok=True)
                            code = 1
                            last_line = check["detail"]
                    else:
                        video["temporary"].unlink(missing_ok=True)
                except OSError as error:
                    code = 1
                    last_line = f"Could not save the video. {error}"
            if stopping:
                self.job["status"] = "stopped"
            elif code == 0:
                self.job["status"] = "complete"
                self.job["done"] = self.job["total"]
                self.job["message"] = last_line
            else:
                self.job["status"] = "failed"
                self.job["message"] = last_line or f"Process exited with status {code}."
            self.job["finished"] = time.time()
            self.process = None

    @staticmethod
    def signal(process, sig):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    def stop(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                self.job["status"] = "stopping"
                self.signal(self.process, signal.SIGTERM)

    def state(self):
        with self.lock:
            return dict(self.job) if self.job else None


JOBS = JobManager()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def response(self, content_type, length):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def json(self, payload):
        data = json.dumps(payload, allow_nan=False).encode()
        self.response("application/json", len(data))
        self.wfile.write(data)

    def file(self, path):
        with path.open("rb") as stream:
            self.response(mimetypes.guess_type(path)[0] or "application/octet-stream", path.stat().st_size)
            shutil.copyfileobj(stream, self.wfile)

    def local_hosts(self):
        return {f"{host}:{self.server.server_port}" for host in (LOCAL, "localhost")}

    def do_GET(self):
        if self.headers.get("Host") not in self.local_hosts():
            self.send_error(403)
            return
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        try:
            if url.path in {"/", "/app.js", "/styles.css"}:
                self.file(UI / (url.path[1:] or "index.html"))
            elif url.path == "/api/setup":
                self.json(clean({
                    "defaults": PLAN_DEFAULTS, "workers": DEFAULT_WORKERS, "video_format": VIDEO_FORMAT,
                    "max_workers": os.cpu_count() or 1, "figures": FIGURES, "data_files": DATA_FILES,
                    "presets": {
                        p.stem: json.loads(p.read_text()) for p in sorted((ROOT / "configs").glob("*.json"))
                    },
                }))
            elif url.path == "/api/state":
                self.json({"job": clean(JOBS.state()), "results": list_results()})
            elif url.path == "/api/run":
                self.json(run_details(find_result(query["key"][0])))
            elif url.path == "/api/frames":
                directory = find_result(query["key"][0])
                offset = max(0, int(query.get("offset", ["0"])[0]))
                frames = []
                path = directory / "frames.jsonl"
                if path.exists():
                    with path.open("rb") as stream:
                        stream.seek(offset)
                        for _ in range(400):
                            row = stream.readline()
                            if not row.endswith(b"\n"):
                                break
                            frames.append(json.loads(row))
                            offset = stream.tell()
                self.json({"offset": offset, "frames": clean(frames)})
            elif url.path == "/api/file":
                directory = find_result(query["key"][0])
                name = query["name"][0]
                if name in FIGURES:
                    self.file(figure_directory(directory) / name)
                elif VIDEO_NAME.fullmatch(name):
                    self.file(video_folder(directory) / name)
                elif re.fullmatch(r"(?:N\d+_rc[\w.]+_trial\d+_robot\d+)\.json", name):
                    self.file(video_folder(directory) / name)
                elif name in DATA_FILES or name in LOGS:
                    self.file(directory / name)
                else:
                    self.send_error(404)
            else:
                self.send_error(404)
        except (KeyError, ValueError, FileNotFoundError):
            self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        # Only the local dashboard itself may start and stop processes.
        origin = self.headers.get("Origin")
        hosts = self.local_hosts()
        if self.headers.get("Host") not in hosts or (origin is not None and origin not in {f"http://{host}" for host in hosts}):
            self.send_error(403)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16_384 or self.headers.get_content_type() != "application/json":
                raise ValueError("Expected a JSON request.")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object.")
            if self.path == "/api/start":
                self.json({"job": clean(JOBS.start(data["action"], data))})
            elif self.path == "/api/stop":
                JOBS.stop()
                self.json({"job": clean(JOBS.state())})
            else:
                self.send_error(404)
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            self.json({"error": str(error)})
        except OSError as error:
            self.json({"error": f"Could not start the operation. {error}"})


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--trial-worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.trial_worker:
        run_live_trial(args.trial_worker)
        return

    with ThreadingHTTPServer((LOCAL, args.port), DashboardHandler) as server:
        print(f"Fieldwork dashboard at http://{LOCAL}:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            JOBS.stop()


if __name__ == "__main__":
    main()
