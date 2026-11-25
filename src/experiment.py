"""Run a sweep of trials described by a JSON config file.

Usage:
    python src/experiment.py configs/pilot.json
    python src/experiment.py configs/main.json --workers 8

Writes to results/<config name>/ (or --out):
    trials.csv       one row per trial: identifiers, seeds' inputs, summary measures
    steps.csv.gz     step-by-step measurements for every trial
    maps/N<n>.npz    the map and start cells of every trial (shared across radii)
    manifest.json    config, master seed, software versions, git commit, timing,
                     and any failed trials (section 10)

Each trial runs in its own process. Seeds are fixed by (master seed, N, trial
index) before anything is scheduled, so the order trials finish in does not
change the results.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import subprocess
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
import scipy

import simulator as sim

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "master_seed": 2026,
    "team_sizes": [1, 2, 4, 8, 16, 32],
    "comm_ranges": [0, 3, 6, 12, "unlimited"],
    "trials": 100,
    "obstacle_density": sim.OBSTACLE_DENSITY,
    "open_cells_per_robot": sim.OPEN_CELLS_PER_ROBOT,
    "replan": "immediate",
    "sensing_radius": sim.SENSING_RADIUS,
    "step_cap_factor": sim.STEP_CAP_FACTOR,
}


def parse_range(value) -> float:
    if isinstance(value, str) and value.lower() in {"unlimited", "inf", "infinity"}:
        return math.inf
    return float(value)


def range_label(r_c: float) -> str:
    if math.isinf(r_c):
        return "unlimited"
    return str(int(r_c)) if float(r_c).is_integer() else str(r_c)


def load_config(path: str | Path) -> dict:
    with open(path) as fh:
        cfg = {**DEFAULTS, **json.load(fh)}
    cfg.setdefault("name", Path(path).stem)
    cfg["comm_ranges"] = [parse_range(r) for r in cfg["comm_ranges"]]
    if cfg["replan"] not in sim.REPLAN_RULES:
        raise ValueError(f"replan must be one of {sim.REPLAN_RULES}")
    return cfg


def build_jobs(cfg: dict) -> list[dict]:
    jobs = []
    for n in cfg["team_sizes"]:
        for k in range(cfg["trials"]):
            for idx, r_c in enumerate(cfg["comm_ranges"]):
                jobs.append(dict(
                    n_robots=n, r_c=r_c, trial_index=k, master_seed=cfg["master_seed"],
                    obstacle_density=cfg["obstacle_density"],
                    open_cells_per_robot=cfg["open_cells_per_robot"],
                    replan=cfg["replan"], sensing_radius=cfg["sensing_radius"],
                    step_cap_factor=cfg["step_cap_factor"],
                    return_map=(idx == 0),   # the map is the same at every radius
                ))
    return jobs


def _run(job: dict) -> dict:
    try:
        return {"job": job, **sim.run_trial(**job)}
    except Exception:                      # keep going; failures are logged
        return {"job": job, "error": traceback.format_exc()}


def git_state() -> dict:
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    try:
        return {"commit": git("rev-parse", "HEAD") or None, "uncommitted_changes": bool(git("status", "--porcelain"))}
    except OSError:
        return {"commit": None, "uncommitted_changes": None}


def main(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", help="path to a JSON config in configs/")
    parser.add_argument("--workers", type=int, default=os.cpu_count(), help="parallel processes")
    parser.add_argument("--out", help="output folder (default results/<config name>)")
    parser.add_argument("--no-steps", action="store_true", help="skip the step-by-step file")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    out = Path(args.out) if args.out else ROOT / "results" / cfg["name"]
    (out / "maps").mkdir(parents=True, exist_ok=True)
    jobs = build_jobs(cfg)
    started = datetime.now(timezone.utc)
    t0 = time.time()
    print(f"{cfg['name']}: {len(jobs)} trials on {args.workers} workers -> {out}")

    summaries, step_frames, maps, failures = [], [], {}, []
    done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_run, job) for job in jobs]
        for fut in as_completed(futures):
            res = fut.result()
            done += 1
            job = res["job"]
            if "error" in res:
                failures.append({
                    "n_robots": job["n_robots"], "r_c": range_label(job["r_c"]),
                    "trial_index": job["trial_index"], "error": res["error"],
                })
            else:
                summaries.append(res["summary"])
                if not args.no_steps:
                    frame = pd.DataFrame(res["steps"])
                    frame.insert(0, "trial", job["trial_index"])
                    frame.insert(0, "r_c", job["r_c"])
                    frame.insert(0, "N", job["n_robots"])
                    step_frames.append(frame)
                if "map" in res:
                    maps[(job["n_robots"], job["trial_index"])] = res["map"]
            if done % max(1, len(jobs) // 20) == 0 or done == len(jobs):
                print(f"  {done}/{len(jobs)} done ({time.time() - t0:.0f}s)", flush=True)

    if summaries:
        trials = pd.DataFrame(summaries).sort_values(["N", "trial", "r_c"]).reset_index(drop=True)
        trials.insert(2, "r_c_label", trials["r_c"].map(range_label))
        trials.to_csv(out / "trials.csv", index=False)
    if step_frames:
        steps = pd.concat(step_frames).sort_values(["N", "r_c", "trial", "t"])
        steps.to_csv(out / "steps.csv.gz", index=False, compression="gzip")

    for n in cfg["team_sizes"]:
        ks = sorted(k for (nn, k) in maps if nn == n)
        if ks:
            np.savez_compressed(
                out / "maps" / f"N{n}.npz",
                trial=np.array(ks),
                grid=np.stack([maps[(n, k)]["grid"] for k in ks]),
                entrance=np.array([maps[(n, k)]["entrance"] for k in ks]),
                starts=np.array([maps[(n, k)]["starts"] for k in ks]),
            )

    manifest = {
        "config": {**cfg, "comm_ranges": [range_label(r) for r in cfg["comm_ranges"]]},
        "master_seed": cfg["master_seed"],
        "seed_rule": "numpy SeedSequence(master_seed, spawn_key=(N, trial)) -> map, starts, one stream per robot",
        "started_utc": started.isoformat(timespec="seconds"),
        "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runtime_seconds": round(time.time() - t0, 1),
        "trials_run": len(summaries),
        "trials_failed": len(failures),
        "failures": failures,
        "git": git_state(),
        "software": {
            "python": sys.version.split()[0], "platform": platform.platform(),
            "numpy": np.__version__, "scipy": scipy.__version__,
            "pandas": pd.__version__, "matplotlib": matplotlib.__version__,
        },
    }
    with open(out / "manifest.json", "w") as fh:
        json.dump(manifest, fh, indent=2, allow_nan=False)
    print(f"Finished in {time.time() - t0:.0f}s; {len(failures)} failed. Results in {out}")
    if failures:
        # Failed trials break the pairing across radii for their (N, trial) block.
        print(f"WARNING: {len(failures)} trial(s) failed; see manifest.json. First error:\n"
              + failures[0]["error"].strip().splitlines()[-1], flush=True)
    if not summaries:
        sys.exit(1)
    return out


if __name__ == "__main__":
    main()
