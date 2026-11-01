#!/usr/bin/env python3
"""Statistics, figures and LaTeX tables for one benchmark results directory.

Usage:
    analyze.py results/<timestamp> [--cost analysis/cost.yaml] [--baseline rust-system]
                                   [--bootstrap 2000] [--allow-dry-run]

Writes results/<timestamp>/analysis/: stats.json, summary CSVs, fig_*.pdf/.png
(IEEE single column, 3.5 in) and table_*.tex (booktabs) for \\input{} in the paper.
Only `measured` runs that exited 0 enter timing statistics; `warmup` runs are
ignored and `nmt` runs feed the native-memory table only.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402
from scipy import stats  # noqa: E402

CONFIG_ORDER = ["rust-system", "rust-mimalloc", "scala-g1", "scala-zgc", "scala-parallel"]
# Categorical slots 1-5 of the validated reference palette, fixed per configuration
# (colour follows the entity). Line styles/markers give a second, print-safe encoding.
COLORS = {"rust-system": "#2a78d6", "rust-mimalloc": "#eb6834", "scala-g1": "#1baf7a",
          "scala-zgc": "#eda100", "scala-parallel": "#e87ba4"}
LINESTYLES = {"rust-system": "-", "rust-mimalloc": (0, (5, 2)), "scala-g1": (0, (1, 1.5)),
              "scala-zgc": (0, (6, 2, 1, 2)), "scala-parallel": (0, (3, 1, 1, 1, 1, 1))}
MARKERS = {"rust-system": "o", "rust-mimalloc": "s", "scala-g1": "^", "scala-zgc": "D", "scala-parallel": "v"}
TEXT, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"
COLUMN_IN = 3.5
MIB = 2**20
QUANTILES = {"p50": 0.50, "p95": 0.95, "p99": 0.99, "p99.9": 0.999}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", type=Path)
    ap.add_argument("--cost", type=Path, default=Path(__file__).with_name("cost.yaml"))
    ap.add_argument("--baseline", default="rust-system")
    ap.add_argument("--bootstrap", type=int, default=2000, help="bootstrap resamples for tail-latency CIs")
    ap.add_argument("--seed", type=int, default=20261023, help="bootstrap RNG seed")
    ap.add_argument("--allow-dry-run", action="store_true",
                    help="analyse a dry run (outputs are watermarked and must not be published)")
    return ap.parse_args()


# --------------------------------------------------------------------------- statistics

def mean_ci(x) -> dict:
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n == 0:
        return {"n": 0, "mean": None, "sd": None, "ci95": None, "cv": None}
    mean = float(np.mean(x))
    sd = float(np.std(x, ddof=1)) if n > 1 else None
    half = float(stats.t.ppf(0.975, n - 1) * sd / math.sqrt(n)) if n > 1 else None
    cv = sd / mean if sd is not None and mean else None  # coefficient of variation
    return {"n": n, "mean": mean, "sd": sd, "ci95": half, "cv": cv}


def cliffs_delta(a, b) -> float:
    a, b = np.asarray(a)[:, None], np.asarray(b)[None, :]
    return float(((a > b).sum() - (a < b).sum()) / (a.size * b.size))


def compare(a, b) -> dict:
    """Configuration `a` vs baseline `b` (independent samples)."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if len(a) < 2 or len(b) < 2:
        return {"ratio": None, "mwu_p": None, "cliffs_delta": None}
    return {"ratio": float(a.mean() / b.mean()),
            "mwu_p": float(stats.mannwhitneyu(a, b, alternative="two-sided").pvalue),
            "cliffs_delta": cliffs_delta(a, b)}


def cluster_bootstrap_quantile(runs: list[np.ndarray], q: float, b: int, rng) -> list[float] | None:
    """95% CI of a pooled quantile, resampling whole runs (batches within a run are correlated)."""
    if len(runs) < 2:
        return None
    est = np.empty(b)
    for i in range(b):
        pick = rng.integers(0, len(runs), len(runs))
        est[i] = np.quantile(np.concatenate([runs[j] for j in pick]), q)
    return [float(np.quantile(est, 0.025)), float(np.quantile(est, 0.975))]


# --------------------------------------------------------------------------- loading

def load(results: Path, allow_dry_run: bool):
    manifest = json.loads((results / "manifest.json").read_text())
    if manifest.get("dry_run") and not allow_dry_run:
        sys.exit(f"{results} is a DRY RUN; refusing to analyse (pass --allow-dry-run for plumbing tests only)")
    runs = pd.read_csv(results / "runs.csv")
    for col in ("dry_run", "steal_flagged"):
        if col in runs:
            runs[col] = runs[col].astype(str).str.lower() == "true"
    return manifest, runs


def watermark(manifest: dict) -> str | None:
    """Text stamped on every figure and table that must not be published (None = publishable)."""
    if manifest.get("dry_run"):
        return "DRY RUN — NOT FOR PUBLICATION"
    if manifest.get("smoke_test"):
        return "SMOKE TEST — NOT FOR PUBLICATION"  # Spot instance (--allow-spot)
    return None


def ordered(configs) -> list[str]:
    configs = list(dict.fromkeys(configs))
    return [c for c in CONFIG_ORDER if c in configs] + sorted(c for c in configs if c not in CONFIG_ORDER)


def read_batches(run_dir: Path) -> pd.DataFrame | None:
    p = run_dir / "batches.csv"
    return pd.read_csv(p) if p.exists() else None


def run_latencies(runs_dir: Path, run_ids) -> tuple[list[np.ndarray], list[np.ndarray], int]:
    """Per run: full-unit latencies (ms; setup + all batches of a full row group) and
    batch latencies (ms). Also returns how many partial (short, last-in-file) units
    were excluded."""
    per_run_units, per_run_batches, partial = [], [], 0
    for run_id in run_ids:
        b = read_batches(runs_dir / run_id)
        if b is None:
            continue
        per_run_batches.append(b["nanos"].to_numpy() / 1e6)
        b["unit"] = b["batch_id"].str.rsplit("-", n=1).str[0]
        u = b.groupby("unit").agg(rows=("rows", "sum"), nanos=("nanos", "sum"), setup=("setup_nanos", "sum"))
        full = u[u["rows"] == u["rows"].max()]
        partial += len(u) - len(full)
        per_run_units.append(((full["nanos"] + full["setup"]) / 1e6).to_numpy())
    return per_run_units, per_run_batches, partial


def read_pauses(run_dir: Path) -> np.ndarray | None:
    p = run_dir / "gc_pauses.csv"
    return pd.read_csv(p)["duration_ns"].to_numpy() / 1e6 if p.exists() else None


# --------------------------------------------------------------------------- figures

def style() -> None:
    plt.rcParams.update({
        "font.family": "serif", "font.serif": ["Times New Roman", "Times", "Nimbus Roman", "DejaVu Serif"],
        "font.size": 8, "axes.labelsize": 8, "axes.titlesize": 8, "legend.fontsize": 7,
        "xtick.labelsize": 7, "ytick.labelsize": 7,
        "axes.edgecolor": MUTED, "axes.labelcolor": TEXT, "xtick.color": MUTED, "ytick.color": MUTED,
        "text.color": TEXT, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.5,
        "axes.axisbelow": True, "axes.spines.top": False, "axes.spines.right": False,
        "lines.linewidth": 1.2, "legend.frameon": False,
        "pdf.fonttype": 42, "ps.fonttype": 42,  # embed TrueType (IEEE PDF eXpress)
        "savefig.dpi": 300, "savefig.bbox": "tight", "savefig.pad_inches": 0.02,
    })


def save(fig, out: Path, name: str, mark: str | None) -> None:
    """`mark`: watermark text from watermark(manifest); None for publishable output."""
    if mark:
        fig.text(0.5, 0.5, mark, ha="center", va="center", rotation=20,
                 fontsize=11, color="#c0392b", alpha=0.35, transform=fig.transFigure)
    for ext in ("pdf", "png"):
        fig.savefig(out / f"{name}.{ext}")
    plt.close(fig)


def fig_cpu(summary: pd.DataFrame, out: Path, dry: bool) -> None:
    fig, ax = plt.subplots(figsize=(COLUMN_IN, 2.0))
    x = np.arange(len(summary))
    ax.bar(x, summary["cpu_s_mean"], yerr=summary["cpu_s_ci95"].fillna(0), width=0.6,
           color=[COLORS.get(c, MUTED) for c in summary.index], edgecolor="white", linewidth=1.0,
           error_kw={"elinewidth": 0.8, "capsize": 2, "ecolor": TEXT})
    for xi, v, ci in zip(x, summary["cpu_s_mean"], summary["cpu_s_ci95"].fillna(0)):
        ax.annotate(f"{v:.0f}", (xi, v + ci), xytext=(0, 3), textcoords="offset points",
                    ha="center", va="bottom", fontsize=6.5, color=MUTED)
    ax.set_xticks(x, summary.index, rotation=20, ha="right")
    ax.set_ylabel("CPU time (s)")
    ax.grid(axis="x", visible=False)
    save(fig, out, "fig_cpu_seconds", dry)


def fig_memory(timelines: dict[str, pd.DataFrame], out: Path, dry: bool) -> None:
    fig, ax = plt.subplots(figsize=(COLUMN_IN, 2.1))
    for c, df in timelines.items():
        ax.plot(df["t_ns"] / 1e9, (df["anon"] + df["shmem"]) / MIB, color=COLORS.get(c, MUTED), linestyle=LINESTYLES.get(c, "-"),
                label=c)
    ax.set_xlabel("Time since container start (s)")
    ax.set_ylabel("Private memory (MiB)")
    ax.set_ylim(bottom=0)
    ax.legend(loc="center left", bbox_to_anchor=(1.0, 0.5))
    save(fig, out, "fig_memory_timeline", dry)


def fig_latency_cdf(latency: dict[str, np.ndarray], out: Path, dry: bool, name: str, xlabel: str) -> None:
    probs = np.concatenate([np.linspace(0, 0.99, 400), 1 - np.logspace(-2, -5, 200)])
    fig, ax = plt.subplots(figsize=(COLUMN_IN, 2.1))
    for c, values in latency.items():
        qs = np.quantile(values, probs[probs <= 1 - 1 / max(len(values), 1)])
        ax.plot(qs, probs[: len(qs)], color=COLORS.get(c, MUTED), linestyle=LINESTYLES.get(c, "-"), label=c)
    ax.set_xscale("log")
    ax.set_xlabel(xlabel)
    ax.set_ylabel("Cumulative fraction")
    ax.set_ylim(0, 1.0)
    ax.legend(loc="lower right")
    save(fig, out, name, dry)


def fig_gc_pauses(pauses: dict[str, np.ndarray], out: Path, dry: bool) -> None:
    pauses = {c: p for c, p in pauses.items() if len(p)}
    if not pauses:
        return
    allp = np.concatenate(list(pauses.values()))
    lo, hi = max(allp.min(), 1e-3), allp.max()
    bins = np.logspace(math.log10(lo) - 0.05, math.log10(hi) + 0.05, 30)
    fig, axes = plt.subplots(len(pauses), 1, figsize=(COLUMN_IN, 0.8 + 0.7 * len(pauses)), sharex=True)
    axes = np.atleast_1d(axes)
    for ax, (c, p) in zip(axes, pauses.items()):
        ax.hist(p, bins=bins, color=COLORS.get(c, MUTED), edgecolor="white", linewidth=0.6)
        ax.set_title(f"{c} (n={len(p)} pauses)", loc="left", fontsize=7, pad=2)
        ax.set_ylabel("Pauses")
        ax.grid(axis="x", visible=False)
    axes[-1].set_xscale("log")
    axes[-1].set_xlabel("GC pause duration (ms)")
    save(fig, out, "fig_gc_pauses", dry)


# --------------------------------------------------------------------------- LaTeX

def fmt(v, digits=1) -> str:
    return "--" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:,.{digits}f}"


def pm(mean, ci, digits=1) -> str:
    return fmt(mean, digits) if ci is None or (isinstance(ci, float) and math.isnan(ci)) \
        else f"{fmt(mean, digits)} $\\pm$ {fmt(ci, digits)}"


def write_table(out: Path, name: str, header: list[str], rows: list[list[str]], note: str,
                mark: str | None) -> None:
    lines = [f"% {name}: generated by analysis/{Path(sys.argv[0]).name} -- do not edit by hand.", f"% {note}"]
    if mark:
        lines.append(f"% {mark}")
    lines += [f"\\begin{{tabular}}{{l{'r' * (len(header) - 1)}}}", "\\toprule",
              " & ".join(header) + " \\\\", "\\midrule"]
    lines += [" & ".join(r) + " \\\\" for r in rows]
    lines += ["\\bottomrule", "\\end{tabular}", ""]
    (out / f"{name}.tex").write_text("\n".join(lines))


# --------------------------------------------------------------------------- main

def main() -> int:
    args = parse_args()
    manifest, runs = load(args.results, args.allow_dry_run)
    dry = watermark(manifest)  # watermark text, or None when publishable
    out = args.results / "analysis"
    out.mkdir(exist_ok=True)
    runs_dir = args.results / "runs"
    rng = np.random.default_rng(args.seed)
    note = f"source: {args.results.name}; git {manifest['git'].get('describe')}"

    measured = runs[(runs["kind"] == "measured") & (runs["exit_code"] == 0)].copy()
    failed = runs[(runs["kind"] == "measured") & (runs["exit_code"] != 0)]
    if measured.empty:
        sys.exit("no successful measured runs")
    measured["cpu_s"] = measured["cpu_usage_usec"] / 1e6
    measured["wall_s"] = measured["wall_ns"] / 1e9
    measured["pipeline_wall_s"] = measured["pipeline_wall_ns"] / 1e9
    for col in ("peak_private", "peak_anon", "peak_shmem", "peak_pagecache", "peak_current", "memory_peak"):
        measured[f"{col}_mib"] = measured[f"{col}_bytes"] / MIB
    configs = ordered(measured["config"])
    cpus = float(manifest["config"]["container"]["cpus"])

    # ---- per-run metrics: mean, sd, 95% CI
    # M_peak = peak_private = max(anon + shmem); see bench/run.py.
    metrics = ["cpu_s", "wall_s", "pipeline_wall_s", "peak_private_mib", "peak_anon_mib", "peak_shmem_mib",
               "peak_pagecache_mib", "peak_current_mib", "memory_peak_mib"]
    result: dict = {"source": str(args.results), "dry_run": dry, "baseline": args.baseline,
                    "failed_measured_runs": failed["run_id"].tolist(), "configs": {}}
    rows = []
    for c in configs:
        sub = measured[measured["config"] == c]
        entry = {m: mean_ci(sub[m]) for m in metrics}
        result["configs"][c] = entry
        rows.append({"config": c, **{f"{m}_{k}": entry[m][k] for m in metrics
                                     for k in ("n", "mean", "sd", "ci95", "cv")}})
    summary = pd.DataFrame(rows).set_index("config")
    summary.to_csv(out / "summary_runs.csv")

    # ---- comparisons against the baseline
    base = measured[measured["config"] == args.baseline]
    comparisons = {}
    for c in configs:
        if c == args.baseline or base.empty:
            continue
        sub = measured[measured["config"] == c]
        comparisons[c] = {m: compare(sub[m], base[m]) for m in ("cpu_s", "wall_s", "peak_private_mib")}
    result["comparisons"] = comparisons

    # ---- VM noise: CPU steal on the run's cores (flag threshold from configs.yaml)
    # Sensitivity analysis: the same statistics with flagged runs excluded.
    has_steal = "steal_flagged" in measured and measured["steal_fraction"].notna().any()
    steal_rows = []
    if has_steal:
        for c in configs:
            sub = measured[measured["config"] == c]
            clean = sub[~sub["steal_flagged"]]
            entry = {"runs": int(len(sub)), "flagged": int(sub["steal_flagged"].sum()),
                     "steal_fraction_mean": float(sub["steal_fraction"].mean()),
                     "steal_fraction_max": float(sub["steal_fraction"].max()),
                     **{f"{m}_excl_flagged": mean_ci(clean[m]) for m in ("cpu_s", "wall_s", "peak_private_mib")}}
            result["configs"][c]["steal"] = entry
            steal_rows.append({"config": c, **{k: v for k, v in entry.items() if not isinstance(v, dict)}})
        pd.DataFrame(steal_rows).to_csv(out / "summary_steal.csv", index=False)
        result["steal_flag_fraction"] = manifest["config"]["protocol"].get("steal_flag_fraction")

    # ---- latency
    # PRIMARY: per-unit latency = setup_nanos + sum of batch nanos of one row-group
    # unit (65,536 rows). Same scope in both languages: parquet-java loads the row
    # group during setup, arrow-rs reads pages lazily inside batches; the unit sum
    # covers both. Only full-size units count (each file's last row group is short).
    # SECONDARY: per-batch latency = `nanos` of one 8,192-row batch (asymmetric I/O
    # placement, see PLAN.md §3).
    unit_lat, batch_lat, unit_rows_out, batch_rows_out = {}, {}, [], []
    for c in configs:
        per_run_units, per_run_batches, partial = run_latencies(runs_dir,
                                                                measured.loc[measured["config"] == c, "run_id"])
        if not per_run_units:
            continue
        for store, rows_out, per_run, label, extra in (
                (unit_lat, unit_rows_out, per_run_units, "unit_latency_ms", {"partial_units_excluded": partial}),
                (batch_lat, batch_rows_out, per_run_batches, "batch_latency_ms", {})):
            pooled = np.concatenate(per_run)
            store[c] = pooled
            entry = {k: float(np.quantile(pooled, q)) for k, q in QUANTILES.items()}
            entry.update({"samples": int(pooled.size), "runs": len(per_run), **extra,
                          "p99_ci95": cluster_bootstrap_quantile(per_run, 0.99, args.bootstrap, rng),
                          "p99.9_ci95": cluster_bootstrap_quantile(per_run, 0.999, args.bootstrap, rng)})
            result["configs"][c][label] = entry
            rows_out.append({"config": c, **{k: v for k, v in entry.items() if not k.endswith("ci95")}})
    pd.DataFrame(unit_rows_out).to_csv(out / "summary_latency_unit.csv", index=False)
    pd.DataFrame(batch_rows_out).to_csv(out / "summary_latency_batch.csv", index=False)

    # ---- GC pauses (JVM configs)
    pauses, gc_rows = {}, []
    for c in configs:
        per_run = [p for r in measured.loc[measured["config"] == c, "run_id"]
                   if (p := read_pauses(runs_dir / r)) is not None]
        if not per_run:
            continue
        pooled = np.concatenate(per_run) if per_run else np.array([])
        pauses[c] = pooled
        sub = measured[measured["config"] == c]
        share = (sub["gc_pause_total_ns"] / sub["wall_ns"]).to_numpy() * 100
        entry = {
            "pauses": int(pooled.size),
            **{f"{k}_ms": (float(np.quantile(pooled, q)) if pooled.size else None)
               for k, q in QUANTILES.items() if k != "p99.9"},
            "max_ms": float(pooled.max()) if pooled.size else None,
            "gc_count_per_run": mean_ci(sub["gc_count"]),
            "pause_total_s_per_run": mean_ci(sub["gc_pause_total_ns"] / 1e9),
            "pause_share_pct": mean_ci(share),
            "heap_used_max_mib": mean_ci(sub["heap_used_max"] / MIB),
            "heap_committed_max_mib": mean_ci(sub["heap_committed_max"] / MIB),
        }
        result["configs"][c]["gc"] = entry
        gc_rows.append({"config": c, **{k: v for k, v in entry.items() if not isinstance(v, dict)}})
    pd.DataFrame(gc_rows).to_csv(out / "summary_gc.csv", index=False)

    # ---- native memory tracking (separate runs, not timed)
    nmt = runs[(runs["kind"] == "nmt") & (runs["exit_code"] == 0)]
    result["nmt"] = {c: {"total_committed_mib": float(g["nmt_total_committed_bytes"].mean() / MIB),
                         "heap_committed_mib": float(g["nmt_heap_committed_bytes"].mean() / MIB),
                         "peak_private_mib": float(g["peak_private_bytes"].mean() / MIB), "runs": int(len(g))}
                     for c, g in nmt.groupby("config")} if not nmt.empty else {}

    # ---- cost model
    cost_cfg = yaml.safe_load(args.cost.read_text()) if args.cost.exists() else {}
    rate = (cost_cfg.get("rates") or {}).get("vcpu_hour")
    vcpu_h = {}
    for c in configs:
        sub = measured[measured["config"] == c]
        vcpu_h[c] = {"provisioned": mean_ci(cpus * sub["wall_s"] / 3600),
                     "consumed": mean_ci(sub["cpu_s"] / 3600)}
    result["vcpu_hours"] = vcpu_h
    if rate is not None:
        result["cost"] = {"currency": cost_cfg.get("currency"), "vcpu_hour": rate,
                          "reference": cost_cfg.get("reference"),
                          "per_run": {c: {k: v["mean"] * rate for k, v in h.items()} for c, h in vcpu_h.items()}}
    else:
        result["cost"] = None
        print("cost: analysis/cost.yaml has no vcpu_hour rate; cost table not emitted", file=sys.stderr)

    (out / "stats.json").write_text(json.dumps(result, indent=2))

    # ---- figures
    style()
    fig_cpu(summary, out, dry)
    timelines = {}
    for c in configs:
        sub = measured[measured["config"] == c].sort_values("wall_ns")
        if sub.empty:
            continue
        rep = sub.iloc[(len(sub) - 1) // 2]["run_id"]  # median-wall run
        p = runs_dir / rep / "cgroup_samples.csv"
        if p.exists():
            df = pd.read_csv(p)
            if not df.empty:
                timelines[c] = df
    if timelines:
        fig_memory(timelines, out, dry)
    if unit_lat:
        fig_latency_cdf(unit_lat, out, dry, "fig_latency_unit_cdf", "Per-unit latency (ms, 65,536-row row group)")
        fig_latency_cdf(batch_lat, out, dry, "fig_latency_batch_cdf", "Per-batch latency (ms, 8,192 rows)")
    fig_gc_pauses(pauses, out, dry)

    # ---- LaTeX tables
    def cv(c, m):
        v = summary.loc[c, f"{m}_cv"]
        return "--" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v * 100:.1f}"

    write_table(out, "table_runtime",
                ["Configuration", "$n$", "CPU (s)", "CV (\\%)", "Wall (s)", "CV (\\%)",
                 "$M_{\\mathrm{peak}}$ (MiB)", "Page cache (MiB)"],
                [[c, str(int(summary.loc[c, "cpu_s_n"])), pm(summary.loc[c, "cpu_s_mean"], summary.loc[c, "cpu_s_ci95"]),
                  cv(c, "cpu_s"), pm(summary.loc[c, "wall_s_mean"], summary.loc[c, "wall_s_ci95"]), cv(c, "wall_s"),
                  pm(summary.loc[c, "peak_private_mib_mean"], summary.loc[c, "peak_private_mib_ci95"], 0),
                  fmt(summary.loc[c, "peak_pagecache_mib_mean"], 0)] for c in configs],
                note + f"; host class {manifest.get('host_class', 'bare-metal')}; mean $\\pm$ 95% CI (t); "
                "CV = coefficient of variation (sd/mean); M_peak = max(anon+shmem), page cache = max(file-shmem); "
                "JVM rows are production-style (-Xms3G -Xmx3G -XX:+AlwaysPreTouch)", dry)
    if steal_rows:
        thr = result["steal_flag_fraction"]
        write_table(out, "table_steal_sensitivity",
                    ["Configuration", "Flagged", "Mean steal (\\%)", "CPU (s) all", "CPU (s) excl.",
                     "Wall (s) all", "Wall (s) excl."],
                    [[c, f"{s['flagged']}/{s['runs']}", f"{s['steal_fraction_mean'] * 100:.2f}",
                      pm(summary.loc[c, "cpu_s_mean"], summary.loc[c, "cpu_s_ci95"]),
                      pm(s["cpu_s_excl_flagged"]["mean"], s["cpu_s_excl_flagged"]["ci95"]),
                      pm(summary.loc[c, "wall_s_mean"], summary.loc[c, "wall_s_ci95"]),
                      pm(s["wall_s_excl_flagged"]["mean"], s["wall_s_excl_flagged"]["ci95"])]
                     for c, s in ((c, result["configs"][c]["steal"]) for c in configs)],
                    note + f"; a run is flagged when CPU steal on its cores exceeds {thr * 100:g}% of its CPU "
                    "time; 'excl.' repeats the statistics without flagged runs (sensitivity analysis)", dry)
    if comparisons:
        write_table(out, "table_comparison",
                    ["Configuration", "CPU ratio", "$\\delta$", "$p$", "$M_{\\mathrm{peak}}$ ratio", "$\\delta$", "$p$"],
                    [[c] + [x for m in ("cpu_s", "peak_private_mib") for x in
                            (fmt(comparisons[c][m]["ratio"], 2), fmt(comparisons[c][m]["cliffs_delta"], 2),
                             fmt(comparisons[c][m]["mwu_p"], 4))] for c in comparisons],
                    note + f"; vs {args.baseline}; Cliff's delta; two-sided Mann-Whitney U", dry)
    def latency_table(name: str, label: str, configs_present, digits: int, what: str) -> None:
        def cell(c, k):
            e = result["configs"][c][label]
            if e["samples"] < 10 / (1 - QUANTILES[k]):  # too few samples for this tail quantile
                return "--"
            ci = e.get(f"{k}_ci95") if k in ("p99", "p99.9") else None
            return fmt(e[k], digits) + (f" [{ci[0]:.{digits}f}, {ci[1]:.{digits}f}]" if ci else "")
        write_table(out, name, ["Configuration", "$n$", "p50 (ms)", "p95 (ms)", "p99 (ms)", "p99.9 (ms)"],
                    [[c, str(result["configs"][c][label]["samples"])] + [cell(c, k) for k in QUANTILES]
                     for c in configs_present],
                    note + f"; {what}; [cluster-bootstrap 95% CI, resampling runs]; "
                    "-- = fewer than 10/(1-q) samples for quantile q", dry)

    if unit_lat:
        latency_table("table_latency_unit", "unit_latency_ms", unit_lat, 1,
                      "PRIMARY per-unit latency = setup + all batches of one 65,536-row row group (full units only)")
        latency_table("table_latency_batch", "batch_latency_ms", batch_lat, 2,
                      "SECONDARY per-batch latency, 8,192 rows; excludes unit setup, so row-group I/O is inside "
                      "batches for Rust (lazy pages) but in setup for Scala (readRowGroup)")
    if gc_rows:
        write_table(out, "table_gc",
                    ["Configuration", "GCs/run", "Pause/run (s)", "Pause share (\\%)", "p50 (ms)", "p99 (ms)",
                     "max (ms)"],
                    [[c, fmt(g["gc_count_per_run"]["mean"], 0), fmt(g["pause_total_s_per_run"]["mean"], 2),
                      fmt(g["pause_share_pct"]["mean"], 2), fmt(g["p50_ms"], 2),
                      fmt(g["p99_ms"], 2) if g["pauses"] >= 1000 else "--", fmt(g["max_ms"], 2)]
                     for c, g in ((c, result["configs"][c]["gc"]) for c in pauses)],
                    note + "; JFR jdk.GCPhasePause, pooled over measured runs; p99 shown only with >= 1000 pauses",
                    dry)
    if result["cost"]:
        cur = result["cost"]["currency"] or ""
        write_table(out, "table_cost",
                    ["Configuration", "vCPU-h provisioned", "vCPU-h consumed", f"Cost prov. ({cur})",
                     f"Cost cons. ({cur})"],
                    [[c, fmt(vcpu_h[c]["provisioned"]["mean"], 4), fmt(vcpu_h[c]["consumed"]["mean"], 4),
                      fmt(result["cost"]["per_run"][c]["provisioned"], 5),
                      fmt(result["cost"]["per_run"][c]["consumed"], 5)] for c in configs],
                    note + f"; rate {rate} {cur}/vCPU-h from analysis/cost.yaml", dry)

    print(f"analysis written to {out}" + (f"  [{dry}]" if dry else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
