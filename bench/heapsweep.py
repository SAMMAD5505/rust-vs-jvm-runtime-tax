#!/usr/bin/env python3
"""Heap-size sweep (Hertz & Berger, OOPSLA 2005 methodology).

Phase 0 - off-heap reserve. For each JVM collector, one NativeMemoryTracking
run at the search upper bound (3,072 MiB) on the sweep dataset measures
non-heap committed memory. reserve_c = 1.5 x that, rounded up to 64 MiB,
floor 512 MiB; heap cap_c = cgroup (4,096 MiB) - reserve_c.

Phase 1 - H_min search. Binary-search the smallest -Xms=-Xmx (granularity
128 MiB) at which the dataset completes. A probe passes only if every attempt
(default 3) exits 0; a Java OutOfMemoryError, a cgroup OOM kill, any other
failure, or a timeout fails the probe. Probes use exactly the sweep's JVM
flags, JFR included.

Phase 2 - sweep. Each collector runs at multiples of its own H_min (default
1.25x, 1.5x, 2x, 3x, 4x); points above cap_c are skipped and recorded with the
reason. Warm-ups first; measured runs of all points and of the Rust
references are then interleaved in random order. Failed runs are counted,
never retried.

Validation (--validate-from DIR, run on a different preset, e.g. medium):
for each collector, probe the H_min found in DIR and ceil(1.25 x H_min),
running all attempts (default 3) at each, and report whether they agree.

All JVM runs here use -Xms = -Xmx and NO -XX:+AlwaysPreTouch. Same host gate
and --dry-run semantics as bench/run.py.

Examples:
    python3 bench/heapsweep.py --data data/small
    python3 bench/heapsweep.py --data data/medium --validate-from results/<ts>-heapsweep
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

import harness as h

SWEEP_COLUMNS = h.RUN_COLUMNS + ["heap_mib", "multiple", "attempt"]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, type=Path, help="dataset directory (default study: small preset)")
    ap.add_argument("--config-file", type=Path, default=h.REPO / "bench" / "configs.yaml")
    ap.add_argument("--collectors", help="comma-separated subset of heapsweep.collectors")
    ap.add_argument("--hmin", help="skip the search for some collectors, e.g. scala-g1=640,scala-zgc=896 (MiB)")
    ap.add_argument("--runs", type=int,
                    help="measured runs per point (default: hosts.<class>.runs, 10 bare-metal / 20 cloud-vm)")
    ap.add_argument("--warmups", type=int, help="warm-up runs per JVM point (overrides configs.yaml)")
    ap.add_argument("--attempts", type=int, help="attempts per H_min probe (overrides configs.yaml)")
    ap.add_argument("--multiples", help="comma-separated heap multiples (overrides configs.yaml)")
    ap.add_argument("--validate-from", type=Path,
                    help="validation mode: probe the H_min values of this heap-sweep results directory")
    ap.add_argument("--results", type=Path, default=h.REPO / "results")
    ap.add_argument("--seed", type=int, help="seed for the randomized run order (default: random, recorded)")
    h.add_host_args(ap)
    return ap.parse_args()


def heap_flags(mib: int) -> list[str]:
    return [f"-Xms{mib}m", f"-Xmx{mib}m"]


def failure_reason(row: dict) -> str | None:
    if row["timed_out"]:
        return "timeout"
    if row["exit_code"] == 0:
        return None
    if row["java_oom"]:
        return "java-OOM"
    if row["oom_killed"]:
        return "cgroup-OOM"
    return f"exit {row['exit_code']}"


def dataset_rows(data: dict) -> int | None:
    return (data.get("datagen") or {}).get("rows")


class Sweep:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        cfg = h.load_yaml(args.config_file)
        hs = cfg["heapsweep"]
        if args.collectors:
            hs["collectors"] = [c for c in hs["collectors"] if c in args.collectors.split(",")]
        for key, val in (("runs", args.runs), ("warmup_runs", args.warmups)):
            if val is not None:
                hs[key] = val
        if hs["runs"] is None:
            hs["runs"] = h.runs_for_host(cfg, args.host_class)
        if args.attempts is not None:
            hs["search"]["attempts"] = args.attempts
        if args.multiples:
            hs["multiples"] = [float(m) for m in args.multiples.split(",")]
        self.cfg, self.hs = cfg, hs
        self.rows: list[dict] = []
        self.seq = 0

    # ------------------------------------------------------------------ plumbing

    def setup(self, label: str) -> None:
        a, cfg = self.args, self.cfg
        self.ctx = h.prepare_host(a, cfg, a.data)
        for ref in cfg["images"].values():
            h.docker("image", "inspect", ref)
        self.data = h.dataset_info(a.data)
        self.preset = (self.data["datagen"] or {}).get("preset", "custom")
        self.seed = a.seed if a.seed is not None else random.SystemRandom().randrange(2**32)
        stamp = h.utc_stamp()
        self.out = a.results / (stamp + f"-{label}" + self.ctx.suffix)
        self.runs_dir = self.out / "runs"
        self.runs_dir.mkdir(parents=True)
        self.manifest = h.base_manifest(cfg, self.ctx, stamp, self.data)
        self.manifest.update({"experiment": label, "schedule_seed": self.seed})
        self.save_manifest()
        h.log(f"{label} -> {self.out}" + ("  [DRY RUN]" if a.dry_run else ""))

    def save_manifest(self) -> None:
        (self.out / "manifest.json").write_text(json.dumps(self.manifest, indent=2))

    def run(self, name: str, kind: str, command: list[str], image: str, *, heap_mib: int | None = None,
            multiple: float | None = None, attempt: int | None = None, timeout_s: float | None = None) -> dict:
        tag = f"{heap_mib}m" if heap_mib else "ref"
        suffix = f"-a{attempt}" if attempt is not None else ""
        run_dir = self.runs_dir / f"{self.seq:04d}-{name}-{kind}-{tag}{suffix}"
        row = h.execute(self.cfg, self.ctx, image=image, command=command, run_dir=run_dir,
                        data=Path(self.data["path"]), timeout_s=timeout_s)
        row.update({"config": name, "kind": kind, "order": self.seq, "preset": self.preset,
                    "heap_mib": heap_mib, "multiple": multiple, "attempt": attempt})
        self.seq += 1
        self.rows.append(row)
        h.write_csv(self.out / "runs.csv", self.rows, SWEEP_COLUMNS)
        h.log(f"    {name} {kind} {tag}" + (f" #{attempt}" if attempt is not None else "") + ": " + h.status_line(row))
        return row

    def gc_flags(self, collector: str) -> list[str]:
        return self.cfg["configs"][collector]["jvm_flags"]

    def sweep_command(self, collector: str, mib: int, *, nmt: bool = False) -> list[str]:
        """The single JVM command shape used by probes, NMT runs and sweep runs."""
        return h.jvm_command(self.cfg, heap_flags(mib), self.gc_flags(collector), jfr=True, nmt=nmt)

    # ------------------------------------------------------------------ phase 0

    def measure_reserve(self, collector: str) -> dict:
        """One NMT run at the search upper bound -> off-heap reserve and heap cap."""
        r = self.hs["reserve"]
        mib = self.hs["search"]["high_mib"]
        h.log(f"[reserve] {collector}: NMT run at {mib} MiB")
        row = self.run(collector, "nmt", self.sweep_command(collector, mib, nmt=True), self.cfg["images"]["scala"],
                       heap_mib=mib)
        total, heap = row.get("nmt_total_committed_bytes"), row.get("nmt_heap_committed_bytes")
        rec = {"nmt_run": row["run_id"], "nmt_heap_mib": mib, "failure": failure_reason(row)}
        if rec["failure"] or total is None or heap is None:
            rec.update({"error": "NMT run failed or produced no summary", "reserve_mib": None, "cap_mib": None})
            return rec
        nonheap = (total - heap) / 2**20
        step = r["round_mib"]
        reserve = max(r["floor_mib"], math.ceil(r["factor"] * nonheap / step) * step)
        rec.update({"nonheap_committed_mib": round(nonheap, 1), "factor": r["factor"], "round_mib": step,
                    "floor_mib": r["floor_mib"], "reserve_mib": reserve,
                    "cap_mib": self.hs["cgroup_mib"] - reserve})
        h.log(f"[reserve] {collector}: non-heap {nonheap:.0f} MiB -> reserve {reserve} MiB, "
              f"heap cap {rec['cap_mib']} MiB")
        return rec

    # ------------------------------------------------------------------ phase 1

    def probe(self, collector: str, mib: int, timeout_s: float | None, *, stop_on_failure: bool = True
              ) -> tuple[bool, list[dict]]:
        attempts = []
        for a in range(self.hs["search"]["attempts"]):
            row = self.run(collector, "probe", self.sweep_command(collector, mib), self.cfg["images"]["scala"],
                           heap_mib=mib, attempt=a, timeout_s=timeout_s)
            attempts.append(row)
            if failure_reason(row) and stop_on_failure:
                break  # one failure fails the probe; skip the remaining attempts
        return all(failure_reason(r) is None for r in attempts), attempts

    @staticmethod
    def probe_record(mib: int, passed: bool, rows: list[dict]) -> dict:
        return {"heap_mib": mib, "passed": passed, "attempts_run": len(rows),
                "attempts_ok": sum(failure_reason(r) is None for r in rows),
                "failures": [{"run_id": r["run_id"], "reason": failure_reason(r)}
                             for r in rows if failure_reason(r)]}

    def search(self, collector: str) -> dict:
        s = self.hs["search"]
        g = s["granularity_mib"]
        lo, hi = s["low_mib"], s["high_mib"] // g * g
        record = {"probes": [], "granularity_mib": g, "attempts": s["attempts"]}

        h.log(f"[search] {collector}: upper bound {hi} MiB")
        passed, rows = self.probe(collector, hi, None)
        record["probes"].append(self.probe_record(hi, passed, rows))
        if not passed:
            record.update({"h_min_mib": None, "error": f"upper bound {hi} MiB failed"})
            return record
        baseline = max(r["wall_ns"] or r["host_wall_ns"] for r in rows) / 1e9
        timeout = max(s["timeout_min_s"], s["timeout_factor"] * baseline)
        record.update({"baseline_wall_s": baseline, "timeout_s": timeout})

        h.log(f"[search] {collector}: lower bound {lo} MiB")
        passed, rows = self.probe(collector, lo, timeout)
        record["probes"].append(self.probe_record(lo, passed, rows))
        if passed:
            record["h_min_mib"] = lo
            return record
        while hi - lo > g:  # invariant: lo fails, hi passes
            mid = lo + max(g, (hi - lo) // 2 // g * g)
            h.log(f"[search] {collector}: probe {mid} MiB (fail {lo}, pass {hi})")
            passed, rows = self.probe(collector, mid, timeout)
            record["probes"].append(self.probe_record(mid, passed, rows))
            if passed:
                hi = mid
            else:
                lo = mid
        record["h_min_mib"] = hi
        return record

    # ------------------------------------------------------------------ phase 2

    def points(self, hmin: dict[str, int], caps: dict[str, int]) -> tuple[list[dict], list[dict]]:
        points, skipped = [], []
        for collector, base in hmin.items():
            cap = caps[collector]
            for m in self.hs["multiples"]:
                mib = math.ceil(m * base)
                p = {"collector": collector, "multiple": m, "heap_mib": mib}
                if mib <= cap:
                    points.append(p)
                else:
                    skipped.append({**p, "reason": f"heap {mib} MiB > cap {cap} MiB "
                                                   f"(cgroup {self.hs['cgroup_mib']} MiB - measured reserve)"})
        return points, skipped

    def sweep(self, points: list[dict], timeouts: dict[str, float | None]) -> None:
        hs, scala = self.hs, self.cfg["images"]["scala"]
        warm = [(p, "warmup") for p in points for _ in range(hs["warmup_runs"])]
        measured = [(p, "measured") for p in points for _ in range(hs["runs"])]
        measured += [({"collector": r, "reference": True}, "measured")
                     for r in hs["references"] for _ in range(hs["runs"])]
        random.Random(self.seed).shuffle(measured)
        schedule = warm + measured
        self.manifest["sweep_schedule"] = [
            f"{p['collector']}:{p.get('heap_mib', 'ref')}:{kind}" for p, kind in schedule]
        self.save_manifest()
        for i, (p, kind) in enumerate(schedule):
            h.log(f"[sweep {i + 1}/{len(schedule)}]")
            if p.get("reference"):
                conf = self.cfg["configs"][p["collector"]]
                self.run(p["collector"], kind, h.rust_command(self.cfg, conf), self.cfg["images"][conf["image"]])
            else:
                self.run(p["collector"], kind, self.sweep_command(p["collector"], p["heap_mib"]), scala,
                         heap_mib=p["heap_mib"], multiple=p["multiple"], timeout_s=timeouts.get(p["collector"]))

    def finish(self, failed_key: str, failed: list[str]) -> dict:
        h.close_manifest(self.manifest)
        self.manifest["output_check"] = h.check_outputs(self.rows, self.runs_dir)
        self.manifest[failed_key] = failed
        self.save_manifest()
        return self.manifest["output_check"]

    # ------------------------------------------------------------------ main (sweep)

    def main(self) -> int:
        self.setup("heapsweep")
        overrides = dict(kv.split("=") for kv in self.args.hmin.split(",")) if self.args.hmin else {}
        search, hmin, caps, timeouts = {}, {}, {}, {}
        for c in self.hs["collectors"]:
            reserve = self.measure_reserve(c)
            if c in overrides:
                rec = {"h_min_mib": int(overrides[c]), "source": "--hmin override"}
            else:
                rec = self.search(c)
            rec["reserve"] = reserve
            search[c] = rec
            if rec.get("h_min_mib") and reserve.get("cap_mib"):
                hmin[c], caps[c] = rec["h_min_mib"], reserve["cap_mib"]
            timeouts[c] = rec.get("timeout_s")
            h.log(f"[search] {c}: H_min = {rec.get('h_min_mib')} MiB; heap cap = {reserve.get('cap_mib')} MiB")
            self.manifest["hmin_search"] = search
            self.save_manifest()
        (self.out / "hmin.json").write_text(json.dumps(search, indent=2))

        points, skipped = self.points(hmin, caps)
        self.manifest.update({"sweep_points": points, "skipped_points": skipped, "heap_caps_mib": caps})
        for p in skipped:
            h.log(f"[sweep] skip {p['collector']} {p['multiple']}x: {p['reason']}")
        self.sweep(points, timeouts)

        h.log("parsing JFR recordings")
        for row in self.rows:
            if row["kind"] in ("measured", "warmup") and h.is_jvm(self.cfg["configs"][row["config"]]):
                row.update(h.parse_jfr(self.cfg, self.runs_dir / row["run_id"]))
        h.write_csv(self.out / "runs.csv", self.rows, SWEEP_COLUMNS)

        failed = [r["run_id"] for r in self.rows if r["kind"] in ("measured", "warmup") and r["exit_code"] != 0]
        check = self.finish("failed_sweep_runs", failed)
        h.log(f"H_min: {hmin}; heap caps: {caps}")
        h.log(f"outputs compared: {check['compared']}, bit-identical: {check['identical']}")
        h.log(f"failed sweep runs: {len(failed)} (counted per point, not retried); results in {self.out}")
        return 0 if check["identical"] is not False else 1

    # ------------------------------------------------------------------ validation

    def validate(self) -> int:
        src = self.args.validate_from
        src_manifest = json.loads((src / "manifest.json").read_text())
        src_hmin = json.loads((src / "hmin.json").read_text())
        self.setup("heapsweep-validate")
        src_rows, val_rows = dataset_rows(src_manifest["dataset"]), dataset_rows(self.data)
        scale = (val_rows / src_rows) if src_rows and val_rows else None
        self.manifest.update({"validate_from": str(src), "source_dataset": src_manifest["dataset"],
                              "row_scale": scale})
        report = {}
        for c in self.hs["collectors"]:
            rec = src_hmin.get(c, {})
            base = rec.get("h_min_mib")
            if not base:
                report[c] = {"error": "no H_min in source sweep"}
                continue
            # Timeout: the source probe timeout scaled by the dataset size ratio.
            timeout = rec["timeout_s"] * scale if rec.get("timeout_s") and scale else None
            entry = {"h_min_small_mib": base, "timeout_s": timeout, "probes": []}
            for m in (1.0, 1.25):
                mib = math.ceil(m * base)
                h.log(f"[validate] {c}: {m:g} x H_min = {mib} MiB")
                passed, rows = self.probe(c, mib, timeout, stop_on_failure=False)
                entry["probes"].append({"multiple": m, **self.probe_record(mib, passed, rows)})
            entry["agrees"] = all(p["passed"] for p in entry["probes"])
            report[c] = entry
            self.manifest["validation"] = report
            self.save_manifest()
        (self.out / "validation.json").write_text(json.dumps(report, indent=2))

        failed = [r["run_id"] for r in self.rows if failure_reason(r)]
        check = self.finish("failed_runs", failed)
        for c, e in report.items():
            if "error" in e:
                h.log(f"[validate] {c}: {e['error']}")
                continue
            res = ", ".join(f"{p['multiple']:g}x={p['attempts_ok']}/{p['attempts_run']}" for p in e["probes"])
            h.log(f"[validate] {c}: H_min(small)={e['h_min_small_mib']} MiB; {res}; "
                  + ("AGREES" if e["agrees"] else "DISAGREES - report this"))
        h.log(f"outputs compared: {check['compared']}, bit-identical: {check['identical']}; results in {self.out}")
        return 0


if __name__ == "__main__":
    sweep = Sweep(parse_args())
    sys.exit(sweep.validate() if sweep.args.validate_from else sweep.main())
