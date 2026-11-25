"""Turn a results folder into figures and summary tables (plan section 9).

Usage:
    python src/analysis.py results/pilot
    python src/analysis.py results/main --out figures/main

Writes to figures/<results folder name>/ (or --out):
    wasted_effort_vs_range.png   main figure: final W/F against r_c, one line per N
    coverage_curves.png          median coverage over time, one panel per N
    steps_to_coverage.png        steps to 90 / 99 / 100% coverage, with completion rates
    predictors.png               information age and lambda_2 against W/F (N >= 4)
    summary_by_setting.csv       medians, quartiles, completion rates, bootstrap intervals
    correlations.csv             rank correlations of each predictor with W/F

Medians are reported with 25th and 75th percentiles. Uncertainty for the
median W/F uses block resampling, where a block is one map with its start
cells (one trial index). Results are associations within this simulator,
not proof of cause.

Held-out prediction and the windowed analysis are deliberately not here yet:
Prof. Lee asked to hold off on them until the pilot has been reviewed.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent
MIN_N_FOR_PREDICTORS = 4  # for N = 2, mean lambda_2 is exactly 2 x connected fraction


def range_label(r_c: float) -> str:
    return "∞" if math.isinf(r_c) else f"{r_c:g}"


def load(results_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    trials = pd.read_csv(results_dir / "trials.csv")
    trials["r_c"] = trials["r_c"].astype(float)
    trials["completed"] = trials["stop_reason"] == "full_coverage"
    steps_path = results_dir / "steps.csv.gz"
    steps = pd.read_csv(steps_path) if steps_path.exists() else None
    if steps is not None:
        steps["r_c"] = steps["r_c"].astype(float)
    return trials, steps


def team_colors(team_sizes):
    cmap = plt.get_cmap("viridis")
    k = max(len(team_sizes) - 1, 1)
    return {n: cmap(0.1 + 0.8 * i / k) for i, n in enumerate(team_sizes)}


def range_positions(ranges):
    """Finite ranges at 0, 1, 2, ...; unlimited set apart as its own category."""
    pos, x = {}, 0.0
    for r in ranges:
        if math.isinf(r):
            x += 0.6
        pos[r] = x
        x += 1
    return pos


def iqr(values: pd.Series) -> tuple[float, float, float]:
    v = values.dropna()
    if v.empty:
        return (math.nan,) * 3
    return float(v.median()), float(v.quantile(0.25)), float(v.quantile(0.75))


def bootstrap_median_ci(values: np.ndarray, rng: np.random.Generator, reps: int = 2000) -> tuple[float, float]:
    """95% interval for the median by resampling blocks (one value per trial here)."""
    values = values[~np.isnan(values)]
    if len(values) < 2:
        return math.nan, math.nan
    samples = rng.choice(values, size=(reps, len(values)), replace=True)
    meds = np.median(samples, axis=1)
    return float(np.quantile(meds, 0.025)), float(np.quantile(meds, 0.975))


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def summary_by_setting(trials: pd.DataFrame) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    rows = []
    for (n, r_c), g in trials.groupby(["N", "r_c"]):
        med, q25, q75 = iqr(g["W_per_F"])
        lo, hi = bootstrap_median_ci(g["W_per_F"].to_numpy(float), rng)
        row = {
            "N": n, "r_c": range_label(r_c), "trials": len(g),
            "completion_rate": g["completed"].mean(),
            "W_per_F_median": med, "W_per_F_q25": q25, "W_per_F_q75": q75,
            "W_per_F_median_ci_low": lo, "W_per_F_median_ci_high": hi,
            "W90_per_F_median": iqr(g["W90_per_F"])[0],
            "steps_median": g["steps"].median(),
            "total_movement_median": g["total_movement"].median(),
        }
        for m in (90, 99, 100):
            col = f"t{m}"
            row[f"reached_{m}_rate"] = g[col].notna().mean()
            row[f"{col}_median"] = iqr(g[col])[0]
        for col in ("information_age", "lambda_2", "lambda_2_over_N", "mean_degree",
                    "connected_fraction", "meetings_per_pair_per_100"):
            row[f"{col}_median"] = iqr(g[col])[0]
        rows.append(row)
    return pd.DataFrame(rows)


def spearman(x: pd.Series, y: pd.Series) -> float:
    ok = x.notna() & y.notna()
    if ok.sum() < 3 or x[ok].nunique() < 2 or y[ok].nunique() < 2:
        return math.nan                    # constant measure: not applicable
    return float(stats.spearmanr(x[ok], y[ok]).statistic)


def correlations(trials: pd.DataFrame) -> pd.DataFrame:
    """Rank correlation of each predictor with W/F, within each setting and
    within each N across radii. NaN means not applicable (a constant measure)."""
    predictors = ["information_age", "lambda_2", "mean_degree", "connected_fraction"]
    rows = []
    sub = trials[trials["N"] >= MIN_N_FOR_PREDICTORS]
    for n, g in sub.groupby("N"):
        rows.append({"N": n, "r_c": "all radii", "trials": len(g),
                     **{p: spearman(g[p], g["W_per_F"]) for p in predictors}})
        for r_c, gg in g.groupby("r_c"):
            rows.append({"N": n, "r_c": range_label(r_c), "trials": len(gg),
                         **{p: spearman(gg[p], gg["W_per_F"]) for p in predictors}})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def fig_wasted_effort(trials: pd.DataFrame, path: Path) -> None:
    ranges = sorted(trials["r_c"].unique())
    pos = range_positions(ranges)
    sizes = sorted(trials["N"].unique())
    colors = team_colors(sizes)
    fig, ax = plt.subplots(figsize=(7.5, 4.8))
    for n in sizes:
        g = trials[trials["N"] == n]
        pts = [(pos[r], *iqr(g.loc[g["r_c"] == r, "W_per_F"])) for r in ranges]
        x, med, lo, hi = map(np.array, zip(*pts))
        finite = np.array([not math.isinf(r) for r in ranges])
        ax.plot(x[finite], med[finite], "-o", color=colors[n], label=f"N = {n}", ms=5)
        ax.plot(x[~finite], med[~finite], "o", color=colors[n], ms=5)
        ax.errorbar(x, med, yerr=[med - lo, hi - med], fmt="none", ecolor=colors[n], alpha=0.5, capsize=3)
    if any(math.isinf(r) for r in ranges):
        sep = pos[ranges[-1]] - 0.8
        ax.axvline(sep, color="0.75", lw=1, ls=":")
    ax.set_xticks([pos[r] for r in ranges], [range_label(r) for r in ranges])
    ax.set_xlabel("Communication range $r_c$ (cells)")
    ax.set_ylabel("Wasted effort per open cell, W / F")
    ax.set_title("Wasted effort against communication range\n(median, bars show 25th to 75th percentile)")
    ax.legend(title="Team size", frameon=False, fontsize=9)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def coverage_matrix(group: pd.DataFrame, horizon: int) -> np.ndarray:
    """Trials x steps coverage, holding each run's final value after it stops."""
    out = []
    for _, g in group.groupby("trial"):
        c = g.sort_values("t")["coverage"].to_numpy()
        row = np.full(horizon + 1, c[-1])
        row[: min(len(c), horizon + 1)] = c[: horizon + 1]
        out.append(row)
    return np.array(out)


def fig_coverage(steps: pd.DataFrame, path: Path) -> None:
    sizes = sorted(steps["N"].unique())
    ranges = sorted(steps["r_c"].unique())
    cmap = plt.get_cmap("plasma")
    colors = {r: cmap(0.85 * i / max(len(ranges) - 1, 1)) for i, r in enumerate(ranges)}
    cols = min(3, len(sizes))
    rows = math.ceil(len(sizes) / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(4.2 * cols, 3.2 * rows), squeeze=False, sharey=True)
    for ax, n in zip(axes.flat, sizes):
        g = steps[steps["N"] == n]
        horizon = int(g.groupby(["r_c", "trial"])["t"].max().quantile(0.9))
        for r in ranges:
            m = coverage_matrix(g[g["r_c"] == r], horizon)
            if m.size == 0:
                continue
            t = np.arange(horizon + 1)
            ax.plot(t, np.median(m, axis=0) * 100, color=colors[r], label=f"$r_c$ = {range_label(r)}")
            ax.fill_between(t, np.quantile(m, 0.25, axis=0) * 100, np.quantile(m, 0.75, axis=0) * 100,
                            color=colors[r], alpha=0.12, lw=0)
        ax.set_title(f"N = {n}")
        ax.set_xlabel("Step")
        ax.grid(alpha=0.2)
        ax.spines[["top", "right"]].set_visible(False)
    for ax in axes[:, 0]:
        ax.set_ylabel("Coverage (%)")
    for ax in list(axes.flat)[len(sizes):]:
        ax.axis("off")
    axes.flat[0].legend(frameon=False, fontsize=8, loc="lower right")
    fig.suptitle("Team coverage over time (median, shaded 25th to 75th percentile)")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def fig_milestones(trials: pd.DataFrame, path: Path) -> None:
    ranges = sorted(trials["r_c"].unique())
    pos = range_positions(ranges)
    sizes = sorted(trials["N"].unique())
    colors = team_colors(sizes)
    fig, axes = plt.subplots(1, 3, figsize=(13, 4), sharey=True)
    for ax, m in zip(axes, (90, 99, 100)):
        col = f"t{m}"
        for n in sizes:
            g = trials[trials["N"] == n]
            med = [iqr(g.loc[g["r_c"] == r, col])[0] for r in ranges]
            rate = [g.loc[g["r_c"] == r, col].notna().mean() for r in ranges]
            x = [pos[r] for r in ranges]
            ax.plot(x, med, "-o", color=colors[n], ms=4, label=f"N = {n}")
            for xi, yi, ri in zip(x, med, rate):
                if ri < 1 and not math.isnan(yi):
                    ax.annotate(f"{ri:.0%}", (xi, yi), fontsize=7, xytext=(3, 3), textcoords="offset points")
        ax.set_xticks([pos[r] for r in ranges], [range_label(r) for r in ranges])
        ax.set_title(f"Steps to {m}% coverage")
        ax.set_xlabel("Communication range $r_c$")
        ax.grid(alpha=0.2)
        ax.spines[["top", "right"]].set_visible(False)
    axes[0].set_ylabel("Median steps (runs that reached it)")
    axes[0].legend(frameon=False, fontsize=8)
    fig.suptitle("Labels show the share of runs that reached the target, where below 100%")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def fig_predictors(trials: pd.DataFrame, path: Path) -> None:
    sub = trials[(trials["N"] >= MIN_N_FOR_PREDICTORS)]
    if sub.empty:
        return
    sizes = sorted(sub["N"].unique())
    colors = team_colors(sizes)
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.3), sharey=True)
    for ax, col, label in zip(axes, ("information_age", "lambda_2_over_N"),
                              ("Mean information age (steps)", "Mean $\\lambda_2$ / N")):
        for n in sizes:
            g = sub[sub["N"] == n]
            rho = spearman(g[col], g["W_per_F"])
            tag = "n/a" if math.isnan(rho) else f"{rho:+.2f}"
            ax.scatter(g[col], g["W_per_F"], s=10, alpha=0.6, color=colors[n], label=f"N = {n} (ρ = {tag})")
        ax.set_xlabel(label)
        ax.grid(alpha=0.2)
        ax.legend(frameon=False, fontsize=8, title="Spearman ρ within N")
        ax.spines[["top", "right"]].set_visible(False)
    axes[0].set_ylabel("Wasted effort per open cell, W / F")
    fig.suptitle("Temporal versus snapshot measures as predictors of wasted effort (one dot per trial)")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def main(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", help="a folder written by experiment.py, e.g. results/pilot")
    parser.add_argument("--out", help="output folder (default figures/<results folder name>)")
    args = parser.parse_args(argv)

    results = Path(args.results)
    out = Path(args.out) if args.out else ROOT / "figures" / results.name
    out.mkdir(parents=True, exist_ok=True)
    trials, steps = load(results)

    summary_by_setting(trials).to_csv(out / "summary_by_setting.csv", index=False)
    correlations(trials).to_csv(out / "correlations.csv", index=False)
    fig_wasted_effort(trials, out / "wasted_effort_vs_range.png")
    fig_milestones(trials, out / "steps_to_coverage.png")
    fig_predictors(trials, out / "predictors.png")
    if steps is not None:
        fig_coverage(steps, out / "coverage_curves.png")
    print(f"Figures and tables written to {out}")
    return out


if __name__ == "__main__":
    main()
