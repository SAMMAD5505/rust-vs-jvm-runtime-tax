#!/usr/bin/env python3
"""VM-vs-metal comparison of the production-style results (bench/run.py).

For every configuration other than the baseline (rust-system), computes the
ratio to the baseline on each host for:
  - CPU-seconds        (ratio of means)
  - M_peak             (ratio of means)
  - p99 per-unit latency (ratio of pooled p99s)
with 95% bootstrap CIs (whole runs resampled independently per configuration),
plus the VM/metal ratio-of-ratios and its CI. A ratio-of-ratios whose CI covers
1 means the VM did not measurably distort that comparison.

Both directories must use the same dataset preset (the c6id.metal validation
runs the small preset, so the c6id.4xlarge side must be its small-preset run).

Usage:
    compare_hosts.py --vm results/<ts> --metal results/<ts> [--out DIR] [--allow-dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from analyze import COLUMN_IN, MIB, MUTED, load, ordered, run_latencies, save, style, watermark, write_table

METRICS = [("cpu_s", "CPU-seconds"), ("mpeak_mib", "M_peak"), ("unit_p99_ms", "p99 per-unit latency")]
FIG_LABELS = {"cpu_s": "CPU-seconds", "mpeak_mib": r"$M_{\mathrm{peak}}$", "unit_p99_ms": "p99 per-unit latency"}
HOST_COLORS = {"vm": "#2a78d6", "metal": "#eb6834"}  # categorical slots 1-2 of the validated palette
HOST_MARKERS = {"vm": "o", "metal": "s"}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vm", required=True, type=Path, help="results directory from c6id.4xlarge (cloud-vm)")
    ap.add_argument("--metal", required=True, type=Path, help="results directory from c6id.metal (bare-metal)")
    ap.add_argument("--out", type=Path, help="output directory (default: results/compare-<vm>-vs-<metal>)")
    ap.add_argument("--baseline", default="rust-system")
    ap.add_argument("--bootstrap", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20261031)
    ap.add_argument("--allow-dry-run", action="store_true")
    ap.add_argument("--allow-preset-mismatch", action="store_true")
    return ap.parse_args()


def load_host(path: Path, allow_dry_run: bool) -> dict:
    manifest, runs = load(path, allow_dry_run)
    m = runs[(runs["kind"] == "measured") & (runs["exit_code"] == 0)].copy()
    m["cpu_s"] = m["cpu_usage_usec"] / 1e6
    m["mpeak_mib"] = m["peak_private_bytes"] / MIB
    per_config = {}
    for c, g in m.groupby("config"):
        units, _, _ = run_latencies(path / "runs", g["run_id"])
        per_config[c] = {"cpu_s": g["cpu_s"].to_numpy(), "mpeak_mib": g["mpeak_mib"].to_numpy(), "units": units}
    preset = ((manifest.get("dataset") or {}).get("datagen") or {}).get("preset")
    ec2 = manifest.get("ec2") or {}
    return {"manifest": manifest, "configs": per_config, "preset": preset, "mark": watermark(manifest),
            "label": f"{ec2.get('instance_type') or manifest.get('host_class', '?')} ({path.name})"}


def statistic(d: dict, metric: str, idx=None) -> float:
    """Metric for one configuration; idx = resampled run indices (None = all runs)."""
    if metric == "unit_p99_ms":
        runs = d["units"] if idx is None else [d["units"][i] for i in idx]
        return float(np.quantile(np.concatenate(runs), 0.99))
    x = d[metric] if idx is None else d[metric][idx]
    return float(np.mean(x))


def nruns(d: dict, metric: str) -> int:
    return len(d["units"]) if metric == "unit_p99_ms" else len(d[metric])


def ratio_draws(num: dict, den: dict, metric: str, b: int, rng) -> np.ndarray:
    n1, n2 = nruns(num, metric), nruns(den, metric)
    return np.array([statistic(num, metric, rng.integers(0, n1, n1)) /
                     statistic(den, metric, rng.integers(0, n2, n2)) for _ in range(b)])


def ci(draws: np.ndarray) -> list[float]:
    return [float(np.quantile(draws, 0.025)), float(np.quantile(draws, 0.975))]


def main() -> int:
    args = parse_args()
    vm, metal = load_host(args.vm, args.allow_dry_run), load_host(args.metal, args.allow_dry_run)
    if vm["preset"] != metal["preset"] and not args.allow_preset_mismatch:
        sys.exit(f"preset mismatch: VM {vm['preset']!r} vs metal {metal['preset']!r} "
                 "(compare the same preset; pass --allow-preset-mismatch to override)")
    for side, key in (("scala", "java.runtime.version"), ("rust", "rustc_version")):
        a = ((vm["manifest"].get("image_toolchains") or {}).get(side) or {}).get(key)
        b = ((metal["manifest"].get("image_toolchains") or {}).get(side) or {}).get(key)
        if a != b:
            sys.exit(f"toolchain mismatch between hosts: {side} {key} VM={a!r} metal={b!r}")
    mark = vm["mark"] or metal["mark"]
    out = args.out or args.vm.parent / f"compare-{args.vm.name}-vs-{args.metal.name}"
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)
    base = args.baseline
    if base not in vm["configs"] or base not in metal["configs"]:
        sys.exit(f"baseline {base} missing on one host")
    configs = [c for c in ordered(set(vm["configs"]) & set(metal["configs"])) if c != base]

    rows, table = [], []
    for c in configs:
        for metric, label in METRICS:
            entry = {"config": c, "metric": metric}
            draws = {}
            for host, d in (("vm", vm), ("metal", metal)):
                if metric == "unit_p99_ms" and (not d["configs"][c]["units"] or not d["configs"][base]["units"]):
                    entry[f"{host}_ratio"] = None
                    continue
                entry[f"{host}_ratio"] = statistic(d["configs"][c], metric) / statistic(d["configs"][base], metric)
                draws[host] = ratio_draws(d["configs"][c], d["configs"][base], metric, args.bootstrap, rng)
                entry[f"{host}_ci95"] = ci(draws[host])
            if len(draws) == 2:
                entry["vm_over_metal"] = entry["vm_ratio"] / entry["metal_ratio"]
                entry["vm_over_metal_ci95"] = ci(draws["vm"] / draws["metal"])
            rows.append(entry)

            def cell(v, c_):
                return "--" if v is None else f"{v:.2f} [{c_[0]:.2f}, {c_[1]:.2f}]"
            table.append([c, label, cell(entry.get("vm_ratio"), entry.get("vm_ci95")),
                          cell(entry.get("metal_ratio"), entry.get("metal_ci95")),
                          cell(entry.get("vm_over_metal"), entry.get("vm_over_metal_ci95"))])

    pd.DataFrame([{k: v for k, v in r.items() if not k.endswith("ci95")} for r in rows]).to_csv(
        out / "compare_hosts.csv", index=False)
    (out / "compare_hosts.json").write_text(json.dumps(
        {"vm": str(args.vm), "metal": str(args.metal), "preset": vm["preset"], "baseline": base,
         "vm_label": vm["label"], "metal_label": metal["label"], "rows": rows}, indent=2))

    note = (f"VM = {vm['label']}; metal = {metal['label']}; preset {vm['preset']}; ratios to {base}; "
            "[95% bootstrap CI, runs resampled]; VM/metal CI covering 1 = no measurable VM distortion")
    write_table(out, "table_compare_hosts", ["Configuration", "Metric", "c6id.4xlarge", "c6id.metal", "VM/metal"],
                table, note, mark)

    # ---- figure: one panel per metric, configurations on y, VM vs metal ratios with CIs
    style()
    fig, axes = plt.subplots(len(METRICS), 1, figsize=(COLUMN_IN, 0.6 + 0.45 * len(configs) * len(METRICS)),
                             sharey=True, gridspec_kw={"hspace": 0.55})
    y = np.arange(len(configs))
    for ax, (metric, label) in zip(np.atleast_1d(axes), METRICS):
        for k, host in enumerate(("vm", "metal")):
            sel = [r for r in rows if r["metric"] == metric]
            vals = np.array([r.get(f"{host}_ratio") or np.nan for r in sel])
            lo = np.array([(r.get(f"{host}_ci95") or [np.nan, np.nan])[0] for r in sel])
            hi = np.array([(r.get(f"{host}_ci95") or [np.nan, np.nan])[1] for r in sel])
            ax.errorbar(vals, y + (k - 0.5) * 0.25, xerr=[vals - lo, hi - vals], fmt=HOST_MARKERS[host],
                        color=HOST_COLORS[host], markersize=4, capsize=2, elinewidth=0.8,
                        label=vm["label"].split(" ")[0] if host == "vm" else metal["label"].split(" ")[0])
        ax.axvline(1.0, color=MUTED, linewidth=0.8, linestyle=":")
        ax.set_yticks(y, configs)
        ax.set_title(f"{FIG_LABELS[metric]} ratio to {base}", loc="left", fontsize=7, pad=3)
        ax.grid(axis="y", visible=False)
    np.atleast_1d(axes)[0].legend(loc="lower right", fontsize=6)
    np.atleast_1d(axes)[-1].set_xlabel("Ratio (configuration / baseline)")
    save(fig, out, "fig_compare_hosts", mark)

    print(f"host comparison written to {out}" + (f"  [{mark}]" if mark else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
