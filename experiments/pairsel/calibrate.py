#!/usr/bin/env python3
"""Phase-0 calibration: fit noise parameters from real ledger votes.

Reads a prod ledger JSONL (as served by /api/ledger?full=1), fits a
contaminated Bradley-Terry model to individual juror votes, estimates
council correlation, and writes calibration.json for the Rust simulator.

Usage: python calibrate.py <prod_ledger.jsonl> <out calibration.json>
"""

import json
import sys
from collections import Counter, defaultdict

import numpy as np


def load_votes(path):
    commits, comparisons, votes = {}, {}, []
    for line in open(path):
        row = json.loads(line)
        kind, p = row.get("kind"), row.get("payload", {})
        if kind == "git.commit":
            commits[p["commit_id"]] = p["oid"]
        elif kind == "comparison.input":
            comparisons[p["comparison_id"]] = (
                p["side_a"]["commit_id"], p["side_b"]["commit_id"]
            )
        elif kind == "llm.judgment":
            votes.append(p)
    ordered = sorted(commits, key=lambda cid: commits[cid])
    index = {cid: i for i, cid in enumerate(ordered)}
    rows = []
    for v in votes:
        a, b = comparisons[v["comparison_id"]]
        w, l = (a, b) if v["winner"] == "A" else (b, a)
        num, den = v["ratio"].split(":")
        rows.append({
            "cmp": v["comparison_id"],
            "winner": index[w],
            "loser": index[l],
            "ratio": max(1.0, float(num) / float(den)),
            "model": v["model_id"],
        })
    return len(ordered), rows


def fit_contaminated_bt(n, rows, iters=4000, lr=0.05, ridge=1e-3):
    """MLE for P(vote) = eps/2 + (1-eps) * sigmoid(theta_w - theta_l)."""
    w = np.array([r["winner"] for r in rows])
    l = np.array([r["loser"] for r in rows])
    theta = np.zeros(n)
    logit_eps = np.array(-2.0)  # eps ~ 0.12 start
    m_t, v_t = np.zeros(n + 1), np.zeros(n + 1)
    for t in range(1, iters + 1):
        eps = 1 / (1 + np.exp(-logit_eps))
        d = theta[w] - theta[l]
        s = 1 / (1 + np.exp(-d))
        p = eps / 2 + (1 - eps) * s
        # gradients of mean negative log likelihood
        dp = -1 / (p * len(rows))
        ds = dp * (1 - eps) * s * (1 - s)
        grad_theta = np.zeros(n)
        np.add.at(grad_theta, w, ds)
        np.add.at(grad_theta, l, -ds)
        grad_theta += ridge * theta
        grad_eps = float(np.sum(dp * (0.5 - s))) * eps * (1 - eps)
        g = np.concatenate([grad_theta, [grad_eps]])
        m_t = 0.9 * m_t + 0.1 * g
        v_t = 0.999 * v_t + 0.001 * g * g
        step = lr * m_t / (1 - 0.9 ** t) / (np.sqrt(v_t / (1 - 0.999 ** t)) + 1e-9)
        theta -= step[:n]
        logit_eps -= step[n]
    theta -= theta.mean()
    eps = float(1 / (1 + np.exp(-logit_eps)))
    d = theta[w] - theta[l]
    p = eps / 2 + (1 - eps) / (1 + np.exp(-d))
    return theta, eps, float(np.mean(np.log(p)))


def council_stats(rows):
    by_cmp = defaultdict(list)
    for r in rows:
        by_cmp[r["cmp"]].append(r)
    multi = {c: v for c, v in by_cmp.items() if len(v) >= 2}
    disagree_pairs = agree_pairs = 0
    unanimous = 0
    for votes in multi.values():
        # direction of each vote as canonical "lower index wins?" boolean
        dirs = [(min(v["winner"], v["loser"]) == v["winner"]) for v in votes]
        for i in range(len(dirs)):
            for j in range(i + 1, len(dirs)):
                if dirs[i] == dirs[j]:
                    agree_pairs += 1
                else:
                    disagree_pairs += 1
        if len(set(dirs)) == 1:
            unanimous += 1
    total_pairs = agree_pairs + disagree_pairs
    return {
        "multi_vote_comparisons": len(multi),
        "pairwise_disagreement_rate": disagree_pairs / total_pairs if total_pairs else 0.0,
        "unanimity_rate": unanimous / len(multi) if multi else 1.0,
    }


def calibrate_tau(theta, eps, rows, target_unanimity, seed=7):
    """Grid-fit shared-latent sd tau to match observed unanimity of 3-vote councils."""
    rng = np.random.default_rng(seed)
    by_cmp = defaultdict(list)
    for r in rows:
        by_cmp[r["cmp"]].append(r)
    gaps = np.array([
        abs(theta[v[0]["winner"]] - theta[v[0]["loser"]])
        for v in by_cmp.values() if len(v) >= 3
    ])
    best_tau, best_err = 0.0, 1e9
    for tau in np.arange(0.0, 3.01, 0.1):
        eta = rng.normal(0, tau, size=(len(gaps), 2000))
        p = eps / 2 + (1 - eps) / (1 + np.exp(-(gaps[:, None] + eta)))
        u = np.mean(p ** 3 + (1 - p) ** 3)
        if abs(u - target_unanimity) < best_err:
            best_err, best_tau = abs(u - target_unanimity), float(tau)
    return best_tau


def ratio_tables(theta, rows):
    """Empirical ratio distributions by |gap| tercile x agree/disagree."""
    gaps = np.array([abs(theta[r["winner"]] - theta[r["loser"]]) for r in rows])
    agrees = np.array([theta[r["winner"]] >= theta[r["loser"]] for r in rows])
    ratios = np.array([r["ratio"] for r in rows])
    t1, t2 = np.quantile(gaps, [1 / 3, 2 / 3])
    tables = {}
    for ti, (lo, hi) in enumerate([(-1, t1), (t1, t2), (t2, 1e9)]):
        for agree in (True, False):
            mask = (gaps > lo) & (gaps <= hi) & (agrees == agree)
            vals = ratios[mask]
            if len(vals) == 0:
                vals = np.array([2.0])
            counts = Counter(np.round(vals, 2))
            total = sum(counts.values())
            tables[f"tercile{ti}_{'agree' if agree else 'disagree'}"] = [
                [float(k), c / total] for k, c in sorted(counts.items())
            ]
    return {"gap_terciles": [float(t1), float(t2)], "tables": tables}


def main():
    ledger, out = sys.argv[1], sys.argv[2]
    n, rows = load_votes(ledger)
    theta, eps, ll = fit_contaminated_bt(n, rows)
    stats = council_stats(rows)
    tau = calibrate_tau(theta, eps, rows, stats["unanimity_rate"])
    calib = {
        "source_votes": len(rows),
        "source_commits": n,
        "epsilon": round(eps, 4),
        "council_tau": tau,
        "theta_sd": round(float(theta.std()), 4),
        "mean_loglik": round(ll, 4),
        "council_stats": stats,
        "ratio_model": ratio_tables(theta, rows),
    }
    json.dump(calib, open(out, "w"), indent=2)
    print(json.dumps({k: v for k, v in calib.items() if k != "ratio_model"}, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
