"""Animate a single exploration run.

Usage:
    python src/animate.py --robots 4 --range 6
    python src/animate.py --robots 8 --range unlimited --trial 3 --out figures/run.gif

The left panel is the simulator's view: walls, the cells the team has seen,
every robot with its sensing circle, and communication links. Cells where a
repeated discovery happened (wasted effort) are shaded orange. The right panel
shows what one robot (robot 0 by default) knows: its own map, including what
teammates have shared with it.

Saves a GIF or MP4 and a PNG of the final frame. The run uses the same seeds as
experiment.py, so --robots, --range and --trial reproduce a trial exactly.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.animation import FFMpegWriter, FuncAnimation, PillowWriter
from matplotlib.collections import LineCollection
from matplotlib.patches import Circle

import simulator as sim

ROOT = Path(__file__).resolve().parent.parent

WALL_RGB = np.array([0.16, 0.18, 0.24])
UNSEEN_RGB = np.array([0.80, 0.81, 0.84])
SEEN_RGB = np.array([0.97, 0.97, 0.98])
WASTED_RGB = np.array([0.99, 0.76, 0.48])


def parse_range(text: str) -> float:
    return math.inf if text.lower() in {"unlimited", "inf"} else float(text)


def team_image(truth: np.ndarray, first_seen: np.ndarray, wasted: np.ndarray, t: int) -> np.ndarray:
    seen = (first_seen >= 0) & (first_seen <= t)
    img = np.empty(truth.shape + (3,))
    img[:] = UNSEEN_RGB
    img[seen] = SEEN_RGB
    img[seen & (truth == sim.WALL)] = WALL_RGB
    img[~seen & (truth == sim.WALL)] = WALL_RGB * 0.6 + UNSEEN_RGB * 0.4
    img[wasted & (truth == sim.OPEN)] = WASTED_RGB
    return img


def robot_image(own: np.ndarray) -> np.ndarray:
    img = np.empty(own.shape + (3,))
    img[:] = UNSEEN_RGB
    img[own == sim.OPEN] = SEEN_RGB
    img[own == sim.WALL] = WALL_RGB
    return img


def animate(n_robots, r_c, trial, master_seed, density, open_per_robot, replan,
            watch, out, fps, max_frames, *, sensing_radius=sim.SENSING_RADIUS,
            step_cap_factor=sim.STEP_CAP_FACTOR, save_final=True, report_summary=False):
    true_map, starts, seeds, _ = sim.make_trial_inputs(master_seed, n_robots, trial, density, open_per_robot)
    res = sim.simulate(true_map, starts, seeds, r_c, replan, sensing_radius,
                       step_cap_factor, record=True, watch=watch)
    if report_summary:
        summary = sim.summarize(res)
        evidence = {key: summary[key] for key in ("steps", "W", "stop_reason", "F", "final_coverage", "t90", "t99", "t100")}
        for key in ("t90", "t99", "t100"):
            if math.isnan(evidence[key]):
                evidence[key] = None
        evidence["start_cells"] = ";".join(f"{r},{c}" for r, c in starts)
        print("Trial summary: " + json.dumps(evidence, allow_nan=False), flush=True)
    hist = res.history
    h, w = true_map.shape
    truth = true_map.grid
    first_seen = res.first_seen.reshape(h, w)
    total = len(hist["positions"])
    stride = max(1, math.ceil(total / max_frames))
    frames = list(range(0, total, stride))
    if frames[-1] != total - 1:
        frames.append(total - 1)

    # First repeated discovery per cell, for cumulative shading during replay.
    first_repeat = np.full(h * w, total)
    for t, cells in enumerate(hist["repeated"]):
        first_repeat[cells] = np.minimum(first_repeat[cells], t)
    first_repeat = first_repeat.reshape(h, w)

    colors = plt.get_cmap("tab10" if n_robots <= 10 else "tab20")(np.arange(n_robots) % (10 if n_robots <= 10 else 20))
    fig, (ax_team, ax_robot) = plt.subplots(1, 2, figsize=(11, 5.6))
    for ax in (ax_team, ax_robot):
        ax.set_xticks([])
        ax.set_yticks([])
    img_team = ax_team.imshow(team_image(truth, first_seen, first_repeat == 0, 0), interpolation="nearest")
    img_robot = ax_robot.imshow(robot_image(hist["maps"][0].reshape(h, w)), interpolation="nearest")
    links = LineCollection([], colors="#2f7df6", linewidths=1.6, alpha=0.9)
    ax_team.add_collection(links)
    circles = [Circle((0, 0), hist["sensing_radius"], fill=False, lw=0.6, alpha=0.5, color=colors[i])
               for i in range(n_robots)]
    for c in circles:
        ax_team.add_patch(c)
    start_xy = np.column_stack([hist["positions"][0][:, 1], hist["positions"][0][:, 0]])
    dots = ax_team.scatter(start_xy[:, 0], start_xy[:, 1], s=28, c=colors[:n_robots], edgecolors="black", linewidths=0.5, zorder=3)
    me = ax_robot.scatter(start_xy[watch:watch + 1, 0], start_xy[watch:watch + 1, 1], s=40, c=[colors[watch]], edgecolors="black", linewidths=0.6, zorder=3)
    goal_mark = ax_robot.scatter([], [], s=60, marker="x", c="black", zorder=3)
    for ax in (ax_team, ax_robot):        # keep the map filling each panel
        ax.set_xlim(-0.5, w - 0.5)
        ax.set_ylim(h - 0.5, -0.5)
        ax.set_autoscale_on(False)
    ax_robot.set_title(f"What robot {watch} knows (its own map)")
    title = fig.suptitle("")
    rc_text = "unlimited" if math.isinf(r_c) else f"{r_c:g}"
    fig.text(0.5, 0.02,
             "Orange: repeated discoveries (wasted effort).  Blue lines: communication links.  "
             "Light: seen.  Grey: not yet seen.", ha="center", fontsize=9, color="0.3")

    def draw(k):
        t = frames[k]
        pos = hist["positions"][t]
        xy = np.column_stack([pos[:, 1], pos[:, 0]])
        img_team.set_data(team_image(truth, first_seen, first_repeat <= t, t))
        img_robot.set_data(robot_image(hist["maps"][t].reshape(h, w)))
        dots.set_offsets(xy)
        for c, p in zip(circles, xy):
            c.center = p
        links.set_segments([[xy[i], xy[j]] for i, j in hist["links"][t]])
        me.set_offsets(xy[watch:watch + 1])
        goal = hist["goals"][t][watch]
        goal_mark.set_offsets(np.empty((0, 2)) if goal is None else [[goal[1], goal[0]]])
        cov = res.steps["coverage"][t] * 100
        cov_label = "<100" if cov < 100 and round(cov, 1) == 100 else f"{cov:.1f}"
        wasted = res.steps["wasted_effort"][t]
        groups = res.steps["n_groups"][t]
        ax_team.set_title(f"Team view: coverage {cov_label}%, wasted effort W = {wasted}")
        title.set_text(f"N = {n_robots} robots, communication range r_c = {rc_text}, step {t}"
                       f" ({groups} group{'s' if groups != 1 else ''})")
        return img_team, img_robot, dots, links, me, goal_mark, *circles

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(rect=(0, 0.04, 1, 0.95))
    anim = FuncAnimation(fig, draw, frames=len(frames), blit=False)
    writer = (FFMpegWriter(fps=fps, codec="libx264", extra_args=["-pix_fmt", "yuv420p", "-movflags", "+faststart"])
              if out.suffix.lower() == ".mp4" else PillowWriter(fps=fps))
    anim.save(out, writer=writer)
    png = None
    if save_final:
        draw(len(frames) - 1)
        png = out.with_suffix(".png")
        fig.savefig(png, dpi=140)
    plt.close(fig)
    print(f"{res.stop_reason} after {res.final_step} steps, W = {res.wasted_effort}. Saved {out}"
          + (f" and {png}" if png else ""))
    return out, png


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--robots", type=int, default=4, help="team size N")
    p.add_argument("--range", default="6", help="communication range r_c, or 'unlimited'")
    p.add_argument("--trial", type=int, default=0, help="trial index (picks the map and starts)")
    p.add_argument("--seed", type=int, default=2026, help="master seed")
    p.add_argument("--density", type=float, default=sim.OBSTACLE_DENSITY)
    p.add_argument("--open-per-robot", type=int, default=sim.OPEN_CELLS_PER_ROBOT)
    p.add_argument("--replan", choices=sim.REPLAN_RULES, default="immediate")
    p.add_argument("--watch", type=int, default=0, help="robot shown in the right panel")
    p.add_argument("--fps", type=int, default=12)
    p.add_argument("--max-frames", type=int, default=300, help="longer runs skip steps to fit")
    p.add_argument("--sensing-radius", type=int, default=sim.SENSING_RADIUS)
    p.add_argument("--step-cap-factor", type=int, default=sim.STEP_CAP_FACTOR)
    p.add_argument("--video-only", action="store_true", help="skip the final-frame PNG")
    p.add_argument("--out", help="GIF or MP4 path (default figures/animations/N<n>_rc<r>_trial<k>.gif)")
    p.add_argument("--report-summary", action="store_true", help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    r_c = parse_range(a.range)
    if not 0 <= a.watch < a.robots:
        p.error("--watch must be a robot index between 0 and N-1")
    rc_tag = "unlimited" if math.isinf(r_c) else f"{r_c:g}"
    out = a.out or ROOT / "figures" / "animations" / f"N{a.robots}_rc{rc_tag}_trial{a.trial}.gif"
    animate(a.robots, r_c, a.trial, a.seed, a.density, a.open_per_robot, a.replan,
            a.watch, out, a.fps, a.max_frames, sensing_radius=a.sensing_radius,
            step_cap_factor=a.step_cap_factor, save_final=not a.video_only, report_summary=a.report_summary)


if __name__ == "__main__":
    main()
