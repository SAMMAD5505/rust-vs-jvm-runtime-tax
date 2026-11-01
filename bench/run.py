#!/usr/bin/env python3
"""Run the production-style benchmark matrix (bench/configs.yaml) and record raw results.

JVM configurations here are *production-style*: fixed -Xms3G -Xmx3G with
-XX:+AlwaysPreTouch. The heap-size sensitivity study is bench/heapsweep.py.

Timed runs are only recorded on a bare-metal Linux host with cgroup v2; use
--dry-run anywhere else to exercise the plumbing (results are marked as dry
runs and analysis refuses to publish them). See PLAN.md §7 and
results/README.md for the output layout.

Example:
    python3 bench/run.py --data data/small --warmup-data data/small
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import harness as h


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, type=Path, help="dataset directory (datagen output) on this host")
    ap.add_argument("--warmup-data", type=Path, help="dataset for JVM warm-up runs (default: --data)")
    ap.add_argument("--config-file", type=Path, default=h.REPO / "bench" / "configs.yaml")
    ap.add_argument("--configs", help="comma-separated subset of configs to run")
    ap.add_argument("--runs", type=int,
                    help="measured runs per config (default: hosts.<class>.runs, 10 bare-metal / 20 cloud-vm)")
    ap.add_argument("--warmups", type=int, help="JVM warm-up runs per JVM config (overrides configs.yaml)")
    ap.add_argument("--nmt-runs", type=int, help="NMT runs per JVM config (overrides configs.yaml)")
    ap.add_argument("--results", type=Path, default=h.REPO / "results")
    ap.add_argument("--seed", type=int, help="seed for the randomized run order (default: random, recorded)")
    h.add_host_args(ap)
    return ap.parse_args()


def load_config(args: argparse.Namespace) -> dict:
    cfg = h.load_yaml(args.config_file)
    proto = cfg["protocol"]
    proto["runs"] = args.runs if args.runs is not None else h.runs_for_host(cfg, args.host_class)
    for key, override in (("jvm_warmup_runs", args.warmups), ("nmt_runs", args.nmt_runs)):
        if override is not None:
            proto[key] = override
    if args.configs:
        wanted = args.configs.split(",")
        unknown = set(wanted) - set(cfg["configs"])
        if unknown:
            sys.exit(f"unknown configs: {', '.join(sorted(unknown))}")
        cfg["configs"] = {k: v for k, v in cfg["configs"].items() if k in wanted}
    return cfg


def build_schedule(cfg: dict, seed: int) -> list[tuple[str, str]]:
    proto, configs = cfg["protocol"], cfg["configs"]
    jvm = [name for name, c in configs.items() if h.is_jvm(c)]
    schedule = [(name, "warmup") for name in jvm for _ in range(proto["jvm_warmup_runs"])]
    measured = [(name, "measured") for name in configs for _ in range(proto["runs"])]
    random.Random(seed).shuffle(measured)
    schedule += measured
    schedule += [(name, "nmt") for name in jvm for _ in range(proto["nmt_runs"])]
    return schedule


def container_command(cfg: dict, conf: dict, kind: str) -> list[str]:
    if not h.is_jvm(conf):
        return h.rust_command(cfg, conf)
    return h.jvm_command(cfg, cfg["jvm_common"], conf["jvm_flags"], nmt=kind == "nmt")


def main() -> int:
    args = parse_args()
    cfg = load_config(args)
    ctx = h.prepare_host(args, cfg, args.data)
    for ref in cfg["images"].values():
        h.docker("image", "inspect", ref)  # fail fast if an image is missing

    data = h.dataset_info(args.data)
    warm = h.dataset_info(args.warmup_data) if args.warmup_data else data
    preset = (data["datagen"] or {}).get("preset", "custom")
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**32)
    schedule = build_schedule(cfg, seed)

    stamp = h.utc_stamp()
    out = args.results / (stamp + ctx.suffix)
    runs_dir = out / "runs"
    runs_dir.mkdir(parents=True)

    manifest = h.base_manifest(cfg, ctx, stamp, data)
    manifest.update({
        "experiment": "production",
        "warmup_dataset": warm,
        "commands": {name: {kind: container_command(cfg, conf, kind) for kind in ("measured", "nmt")}
                     for name, conf in cfg["configs"].items()},
        "schedule_seed": seed,
        "schedule": [f"{i:03d}-{n}-{k}" for i, (n, k) in enumerate(schedule)],
    })
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    h.log(f"{len(schedule)} runs ({ctx.host_class}) -> {out}" + ("  [DRY RUN]" if args.dry_run else ""))

    rows: list[dict] = []
    for order, (name, kind) in enumerate(schedule):
        conf = cfg["configs"][name]
        run_dir = runs_dir / f"{order:03d}-{name}-{kind}"
        dataset = Path(warm["path"]) if kind == "warmup" else Path(data["path"])
        h.log(f"[{order + 1}/{len(schedule)}] {name} ({kind})")
        row = h.execute(cfg, ctx, image=cfg["images"][conf["image"]], command=container_command(cfg, conf, kind),
                        run_dir=run_dir, data=dataset)
        row.update({"config": name, "kind": kind, "order": order, "preset": preset})
        rows.append(row)
        h.write_csv(out / "runs.csv", rows, h.RUN_COLUMNS)
        h.log("    " + h.status_line(row))

    h.log("parsing JFR recordings")
    for row in rows:
        if h.is_jvm(cfg["configs"][row["config"]]):
            row.update(h.parse_jfr(cfg, runs_dir / row["run_id"]))
    h.write_csv(out / "runs.csv", rows, h.RUN_COLUMNS)

    h.close_manifest(manifest)
    manifest["output_check"] = h.check_outputs(rows, runs_dir)
    manifest["failed_runs"] = [r["run_id"] for r in rows if r["exit_code"] != 0]
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    check = manifest["output_check"]
    h.log(f"outputs compared: {check['compared']}, bit-identical: {check['identical']}")
    h.log(f"failed runs: {len(manifest['failed_runs'])}; results in {out}")
    return 0 if not manifest["failed_runs"] and check["identical"] is not False else 1


if __name__ == "__main__":
    sys.exit(main())
