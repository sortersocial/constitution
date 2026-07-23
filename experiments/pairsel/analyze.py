#!/usr/bin/env python3
"""Visualize pair-selection experiment results.

Usage: python analyze.py results/main_n161_c10.csv out_prefix
Produces: <prefix>_tv_curves.png, <prefix>_k_effect.png,
<prefix>_budget_to_accuracy.png, <prefix>_summary.csv, and a text report.
"""

import sys
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

PALETTE = {
    "zip": "#d62728",
    "random_pairs": "#7f7f7f",
    "random_matching": "#1f77b4",
    "cycle_chords": "#17becf",
    "swiss": "#2ca02c",
    "merge_sort": "#98df8a",
    "quicksort": "#bcbd22",
    "bt_uncertainty": "#9467bd",
    "bootstrap_inversion": "#8c564b",
    "payout_opt": "#e377c2",
    "explore_refine": "#ff7f0e",
    "top_heavy": "#ffbb78",
}


def mean_ci(x):
    x = np.asarray(x, dtype=float)
    m = x.mean()
    se = x.std(ddof=1) / np.sqrt(len(x)) if len(x) > 1 else 0.0
    return m, 1.96 * se


def main():
    path, prefix = sys.argv[1], sys.argv[2]
    df = pd.read_csv(path)
    n = int(df["n"].iloc[0])
    strategies = [s for s in PALETTE if s in set(df["strategy"])]

    # --- 1. TV vs budget curves (best k per strategy at final budget) ---
    fig, axes = plt.subplots(1, 3, figsize=(19, 6), sharey=True)
    for ax, k in zip(axes, sorted(df["k"].unique())):
        sub = df[df["k"] == k]
        for s in strategies:
            g = sub[sub["strategy"] == s].groupby("votes")["payout_tv"]
            budgets = sorted(g.groups.keys())
            ms, los, his = [], [], []
            for b in budgets:
                m, ci = mean_ci(g.get_group(b))
                ms.append(m), los.append(m - ci), his.append(m + ci)
            x = [b / n for b in budgets]
            ax.plot(x, ms, label=s, color=PALETTE[s],
                    lw=2.4 if s in ("zip", "random_pairs") else 1.6,
                    ls="--" if s == "random_pairs" else "-")
            ax.fill_between(x, los, his, color=PALETTE[s], alpha=0.15)
        ax.set_xscale("log")
        ax.set_xlabel("votes / N (log)")
        ax.set_title(f"k = {k} votes per edge")
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("payout TV (fraction of emission misallocated)")
    axes[-1].legend(fontsize=8, loc="upper right")
    fig.suptitle(f"Payout error vs vote budget — N={n}, "
                 f"C={df['contributors'].iloc[0]}, mean ± 95% CI over "
                 f"{df['rep'].nunique()} replicates")
    fig.tight_layout()
    fig.savefig(f"{prefix}_tv_curves.png", dpi=130)

    # --- 2. k effect: best strategy comparison at equal vote budget ---
    fig, ax = plt.subplots(figsize=(10, 6))
    final = df[df["votes"] == df["votes"].max()]
    mid_budget = min(16 * n, df["votes"].max())
    mid = df[df["votes"] == mid_budget]
    width = 0.25
    xs = np.arange(len(strategies))
    for off, k in zip((-width, 0, width), sorted(df["k"].unique())):
        vals, cis = [], []
        for s in strategies:
            rows = mid[(mid["strategy"] == s) & (mid["k"] == k)]["payout_tv"]
            m, ci = mean_ci(rows) if len(rows) else (np.nan, 0)
            vals.append(m), cis.append(ci)
        ax.bar(xs + off, vals, width, yerr=cis, capsize=2, label=f"k={k}")
    ax.set_xticks(xs)
    ax.set_xticklabels(strategies, rotation=35, ha="right", fontsize=8)
    ax.set_ylabel(f"payout TV at {mid_budget // n}N votes")
    ax.set_title("Votes-per-edge effect at equal vote budget")
    ax.legend()
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(f"{prefix}_k_effect.png", dpi=130)

    # --- 3. budget to reach TV <= 0.02 (median across reps, k=1) ---
    fig, ax = plt.subplots(figsize=(10, 6))
    target = 0.02
    rows_out = []
    for s in strategies:
        needed = []
        for k in sorted(df["k"].unique()):
            sub = df[(df["strategy"] == s) & (df["k"] == k)]
            per_rep = []
            for _, g in sub.groupby("rep"):
                g = g.sort_values("votes")
                ok = g[g["payout_tv"] <= target]
                per_rep.append(ok["votes"].iloc[0] / n if len(ok) else np.nan)
            arr = np.array(per_rep, dtype=float)
            reached = np.isfinite(arr).mean()
            med = np.nanmedian(arr) if reached > 0.5 else np.nan
            needed.append((k, med, reached))
            rows_out.append({"strategy": s, "k": k,
                             "median_budget_multiple_to_tv02": med,
                             "fraction_reaching_tv02": round(reached, 3)})
        k1 = [x for x in needed if x[0] == 1][0]
        ax.bar(s, k1[1] if np.isfinite(k1[1]) else 0,
               color=PALETTE[s],
               hatch="//" if not np.isfinite(k1[1]) else None)
    ax.set_ylabel(f"median votes/N to reach TV ≤ {target} (k=1)")
    ax.set_title("Budget to acceptable payout accuracy (hatched = >50% of reps never reached)")
    plt.xticks(rotation=35, ha="right", fontsize=8)
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(f"{prefix}_budget_to_accuracy.png", dpi=130)

    # --- 4. summary table + paired contrasts vs zip at 16N, k best ---
    summary = []
    zipk = {}
    for k in sorted(df["k"].unique()):
        rows = mid[(mid["strategy"] == "zip") & (mid["k"] == k)]
        zipk[k] = rows.set_index("rep")["payout_tv"]
    for s in strategies:
        for k in sorted(df["k"].unique()):
            rows = mid[(mid["strategy"] == s) & (mid["k"] == k)]
            if not len(rows):
                continue
            m, ci = mean_ci(rows["payout_tv"])
            # paired difference vs incumbent zip at its observed k=3
            base = zipk.get(3, zipk[max(zipk)])
            joined = rows.set_index("rep")["payout_tv"].to_frame("s").join(
                base.to_frame("zip"), how="inner")
            d = joined["s"] - joined["zip"]
            dm, dci = mean_ci(d) if len(d) > 1 else (np.nan, np.nan)
            summary.append({
                "strategy": s, "k": k,
                "tv_at_16N": round(m, 4), "ci95": round(ci, 4),
                "kendall_at_16N": round(rows["kendall"].mean(), 4),
                "top10_at_16N": round(rows["top10"].mean(), 4),
                "delta_vs_zip_k3": round(dm, 4),
                "delta_ci95": round(dci, 4),
                "significant": bool(abs(dm) > dci and dci == dci),
            })
    sdf = pd.DataFrame(summary).sort_values("tv_at_16N")
    sdf.to_csv(f"{prefix}_summary.csv", index=False)
    bdf = pd.DataFrame(rows_out)
    bdf.to_csv(f"{prefix}_budget_summary.csv", index=False)
    print(sdf.to_string(index=False))
    print(f"\nwrote {prefix}_tv_curves.png, {prefix}_k_effect.png, "
          f"{prefix}_budget_to_accuracy.png, {prefix}_summary.csv")


if __name__ == "__main__":
    main()
