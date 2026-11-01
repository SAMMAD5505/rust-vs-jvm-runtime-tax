#!/usr/bin/env python3
"""Figures and tables for a heap-sweep results directory (bench/heapsweep.py).

Usage:
    heapsweep.py results/<timestamp>-heapsweep [--allow-dry-run]

Writes <dir>/analysis/: stats_heapsweep.json, summary_heapsweep.csv,
fig_heapsweep_cpu.{pdf,png}, fig_heapsweep_mpeak.{pdf,png},
table_heapsweep_hmin.tex, table_heapsweep.tex.
Only `measured` runs that exited 0 enter the statistics; failures are counted
per point. Rust references are drawn as flat lines.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from analyze import (COLORS, COLUMN_IN, LINESTYLES, MARKERS, MIB, MUTED, fmt, load, mean_ci, ordered, pm,
                     read_pauses, save, style, watermark, write_table)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", type=Path)
    ap.add_argument("--allow-dry-run", action="store_true",
                    help="analyse a dry run (outputs are watermarked and must not be published)")
    ap.add_argument("--validation", type=Path,
                    help="results/<ts>-heapsweep-validate directory: also emit table_heapsweep_validate.tex")
    return ap.parse_args()


def validation_table(vdir: Path, out: Path, allow_dry_run: bool) -> None:
    vmanifest, _ = load(vdir, allow_dry_run)
    report = json.loads((vdir / "validation.json").read_text())
    dry = watermark(vmanifest)
    preset = (vmanifest["dataset"].get("datagen") or {}).get("preset", "?")
    rows = []
    for c in ordered(report):
        e = report[c]
        if "error" in e:
            rows.append([c, "--", "--", "--", e["error"]])
            continue
        res = {p["multiple"]: f"{p['attempts_ok']}/{p['attempts_run']}" for p in e["probes"]}
        rows.append([c, fmt(e["h_min_small_mib"], 0), res.get(1.0, "--"), res.get(1.25, "--"),
                     "yes" if e["agrees"] else "\\textbf{no}"])
    write_table(out, "table_heapsweep_validate",
                ["Collector", "$H_{\\min}$ small (MiB)", f"$1\\times$ on {preset}", f"$1.25\\times$ on {preset}",
                 "Agrees"], rows,
                f"source: {vdir.name}; attempts completed / attempts run at H_min and ceil(1.25 x H_min) "
                "from the small-preset search, same JVM flags; all attempts run", dry)


def sweep_figure(points: pd.DataFrame, refs: dict, metric: str, ylabel: str, out: Path, name: str,
                 dry: bool) -> None:
    fig, ax = plt.subplots(figsize=(COLUMN_IN, 2.2))
    for c in ordered(points["collector"]):
        p = points[points["collector"] == c].sort_values("multiple")
        ax.errorbar(p["multiple"], p[f"{metric}_mean"], yerr=p[f"{metric}_ci95"].fillna(0),
                    color=COLORS.get(c, MUTED), linestyle=LINESTYLES.get(c, "-"), marker=MARKERS.get(c, "o"),
                    markersize=4, capsize=2, elinewidth=0.8, label=c)
    # Canonical order, so a dashed reference is drawn over a solid one when they coincide.
    for r in ordered(refs):
        v = refs[r][metric]["mean"]
        if v is None:
            continue
        ax.axhline(v, color=COLORS.get(r, MUTED), linestyle=LINESTYLES.get(r, "--"), linewidth=1.0, label=r)
    multiples = sorted(points["multiple"].unique())
    ax.set_xticks(multiples, [f"{m:g}" for m in multiples])
    ax.set_xlabel(r"Heap size ($\times\,H_{\min}$ of each collector)")
    ax.set_ylabel(ylabel)
    ax.set_ylim(bottom=0)
    handles, labels = ax.get_legend_handles_labels()
    order = {name: i for i, name in enumerate(ordered(labels))}
    pairs = sorted(zip(handles, labels), key=lambda hl: order[hl[1]])
    ax.legend([hl[0] for hl in pairs], [hl[1] for hl in pairs], loc="center left", bbox_to_anchor=(1.0, 0.5))
    save(fig, out, name, dry)


def main() -> int:
    args = parse_args()
    manifest, runs = load(args.results, args.allow_dry_run)
    if manifest.get("experiment") != "heapsweep":
        sys.exit(f"{args.results} is not a heap-sweep results directory")
    dry = watermark(manifest)
    out = args.results / "analysis"
    out.mkdir(exist_ok=True)
    runs_dir = args.results / "runs"
    note = f"source: {args.results.name}; git {manifest['git'].get('describe')}"
    hmin = json.loads((args.results / "hmin.json").read_text())

    runs = runs[runs["kind"] == "measured"].copy()
    runs["cpu_s"] = runs["cpu_usage_usec"] / 1e6
    runs["wall_s"] = runs["wall_ns"] / 1e9
    runs["mpeak_mib"] = runs["peak_private_bytes"] / MIB
    jvm = runs[runs["heap_mib"].notna()]
    rust = runs[runs["heap_mib"].isna()]

    # ---- per sweep point
    rows = []
    for (c, m), g in jvm.groupby(["config", "multiple"]):
        ok = g[g["exit_code"] == 0]
        pauses = [p for r in ok["run_id"] if (p := read_pauses(runs_dir / r)) is not None]
        pooled = np.concatenate(pauses) if pauses else np.array([])
        row = {"collector": c, "multiple": float(m), "heap_mib": int(g["heap_mib"].iloc[0]),
               "h_min_mib": hmin.get(c, {}).get("h_min_mib"), "runs_ok": len(ok), "runs_failed": len(g) - len(ok)}
        for metric, series in (("cpu_s", ok["cpu_s"]), ("wall_s", ok["wall_s"]), ("mpeak_mib", ok["mpeak_mib"]),
                               ("gc_pause_total_s", ok["gc_pause_total_ns"] / 1e9),
                               ("gc_cycle_total_s", ok["gc_duration_total_ns"] / 1e9),
                               ("gc_count", ok["gc_count"])):
            s = mean_ci(series)
            row.update({f"{metric}_mean": s["mean"], f"{metric}_ci95": s["ci95"], f"{metric}_sd": s["sd"]})
        for k, q in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99)):
            row[f"gc_pause_{k}_ms"] = float(np.quantile(pooled, q)) if pooled.size else None
        row["gc_pause_max_ms"] = float(pooled.max()) if pooled.size else None
        row["gc_pauses"] = int(pooled.size)
        rows.append(row)
    points = pd.DataFrame(rows)
    if points.empty:
        sys.exit("no measured heap-sweep runs")
    points.to_csv(out / "summary_heapsweep.csv", index=False)

    # ---- Rust references
    refs = {}
    for c, g in rust.groupby("config"):
        ok = g[g["exit_code"] == 0]
        refs[c] = {"cpu_s": mean_ci(ok["cpu_s"]), "wall_s": mean_ci(ok["wall_s"]),
                   "mpeak_mib": mean_ci(ok["mpeak_mib"]), "runs_failed": int(len(g) - len(ok))}

    (out / "stats_heapsweep.json").write_text(json.dumps(
        {"source": str(args.results), "dry_run": dry, "h_min": hmin, "points": rows, "references": refs,
         "skipped_points": manifest.get("skipped_points", [])}, indent=2, default=float))

    # ---- figures
    style()
    sweep_figure(points, refs, "cpu_s", "CPU time (s)", out, "fig_heapsweep_cpu", dry)
    sweep_figure(points, refs, "mpeak_mib", r"$M_{\mathrm{peak}}$ (MiB)", out, "fig_heapsweep_mpeak", dry)

    # ---- tables
    def reserve(c, key):
        return (hmin[c].get("reserve") or {}).get(key)

    write_table(out, "table_heapsweep_hmin",
                ["Collector", "$H_{\\min}$ (MiB)", "Probes", "Non-heap (MiB)", "Reserve (MiB)", "Heap cap (MiB)"],
                [[c, fmt(hmin[c].get("h_min_mib"), 0), str(len(hmin[c].get("probes", []))),
                  fmt(reserve(c, "nonheap_committed_mib"), 0), fmt(reserve(c, "reserve_mib"), 0),
                  fmt(reserve(c, "cap_mib"), 0)] for c in ordered(hmin)],
                note + "; H_min = smallest -Xms=-Xmx (128 MiB steps) completing all attempts per probe, same "
                "flags as the sweep incl. JFR, no AlwaysPreTouch; Non-heap = NMT committed minus Java heap at "
                "3,072 MiB; Reserve = max(512, 1.5 x non-heap rounded up to 64 MiB); cap = 4,096 - reserve", dry)
    for p in manifest.get("skipped_points", []):
        print(f"skipped: {p['collector']} {p['multiple']}x ({p['heap_mib']} MiB): {p.get('reason')}")
    body = []
    for c in ordered(points["collector"]):
        for _, p in points[points["collector"] == c].sort_values("multiple").iterrows():
            body.append([c, f"{p['multiple']:g}", str(p["heap_mib"]),
                         f"{p['runs_ok']}/{p['runs_ok'] + p['runs_failed']}",
                         pm(p["cpu_s_mean"], p["cpu_s_ci95"]), pm(p["mpeak_mib_mean"], p["mpeak_mib_ci95"], 0),
                         fmt(p["gc_pause_p99_ms"], 2) if p["gc_pauses"] >= 1000 else "--",
                         fmt(p["gc_pause_total_s_mean"], 2),
                         fmt(p["gc_cycle_total_s_mean"], 2)])
    for r in ordered(refs):
        s = refs[r]
        done = s["cpu_s"]["n"]
        body.append([r, "--", "--", f"{done}/{done + s['runs_failed']}", pm(s["cpu_s"]["mean"], s["cpu_s"]["ci95"]),
                     pm(s["mpeak_mib"]["mean"], s["mpeak_mib"]["ci95"], 0), "--", "--", "--"])
    write_table(out, "table_heapsweep",
                ["Config", "$\\times H_{\\min}$", "Heap (MiB)", "Completed", "CPU (s)", "$M_{\\mathrm{peak}}$ (MiB)",
                 "GC p99 (ms)", "Pause (s)", "GC cycles (s)"],
                body, note + "; Completed = successful/total measured runs (failures are not retried; "
                "statistics use successful runs); mean $\\pm$ 95% CI (t); -Xms=-Xmx, no AlwaysPreTouch; Pause = sum of "
                "GCPhasePause per run; GC cycles = sum of GarbageCollection durations per run (wall time, "
                "includes concurrent phases); GC p99 shown only with >= 1000 pooled pauses", dry)

    if args.validation:
        validation_table(args.validation, out, args.allow_dry_run)

    print(f"heap-sweep analysis written to {out}" + (f"  [{dry}]" if dry else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
