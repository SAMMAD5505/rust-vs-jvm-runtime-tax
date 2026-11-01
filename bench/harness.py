"""Shared machinery for bench/run.py and bench/heapsweep.py: host gate, container
runs with cgroup sampling, JFR/NMT parsing, manifests."""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

import hostinfo
import jfrparse
import verify
from sampler import CgroupSampler, container_cgroup

REPO = Path(__file__).resolve().parent.parent
JAR = "/opt/rvj/pipeline-scala.jar"
JFC = "/opt/rvj/gc.jfc"

RUN_COLUMNS = [
    "run_id", "config", "kind", "order", "preset", "dry_run", "exit_code", "oom_killed", "java_oom", "timed_out",
    "wall_ns", "wall_realtime_ns", "host_wall_ns", "pipeline_wall_ns",
    "cpu_usage_usec", "cpu_user_usec", "cpu_system_usec", "cpu_throttled_usec",
    "peak_private_bytes", "peak_anon_bytes", "peak_shmem_bytes", "peak_pagecache_bytes", "peak_file_bytes",
    "peak_kernel_bytes", "peak_current_bytes", "memory_peak_bytes", "samples",
    "rows_read", "rows_kept", "groups",
    "gc_count", "gc_pause_count", "gc_pause_total_ns", "gc_pause_max_ns", "gc_duration_total_ns",
    "z_alloc_stall_total_ns", "heap_used_max", "heap_committed_max", "jdk_version",
    "nmt_total_committed_bytes", "nmt_heap_committed_bytes",
    "steal_usec", "steal_fraction", "steal_flagged", "cpu_mhz_mean", "cpu_mhz_min", "cpu_mhz_max",
]


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def docker(*args: str, check: bool = True) -> str:
    res = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:2])} failed: {res.stderr.strip()}")
    return res.stdout.strip()


def utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def is_jvm(conf: dict) -> bool:
    return conf["image"] == "scala"


# --------------------------------------------------------------------------- host and inputs

def add_host_args(ap) -> None:
    """CLI flags shared by every experiment script."""
    ap.add_argument("--host-class", choices=hostinfo.HOST_CLASSES, default="bare-metal",
                    help="bare-metal (default) or cloud-vm (allowlisted EC2 instance; see docs/aws.md)")
    ap.add_argument("--allow-spot", action="store_true",
                    help="allow a Spot instance (smoke tests only: results are watermarked SMOKE TEST)")
    ap.add_argument("--cpuset", help="explicit --cpuset-cpus list (default: docker/pick-cores.sh)")
    ap.add_argument("--dry-run", action="store_true",
                    help="plumbing test on any host; skips host checks; results are marked DRY RUN")


@dataclass
class HostContext:
    host_class: str
    dry_run: bool
    smoke_test: bool
    drop: bool
    facts: dict
    blockers: list[str]
    cpuset: str | None
    cpuset_source: str
    cpus: list[int] = field(default_factory=list)

    @property
    def suffix(self) -> str:
        return "-dryrun" if self.dry_run else ("-smoke" if self.smoke_test else "")


def runs_for_host(cfg: dict, host_class: str) -> int:
    return cfg["hosts"][host_class.replace("-", "_")]["runs"]


def prepare_host(args, cfg: dict, data_path: Path | None) -> HostContext:
    """Evaluate the host-class gates; exit 2 if timings may not be recorded."""
    want_drop = cfg["protocol"]["drop_caches"]
    facts = hostinfo.collect_facts(data_path)
    blockers = hostinfo.evaluate_gates(args.host_class, facts, cfg["hosts"], allow_spot=args.allow_spot,
                                       want_drop=want_drop)
    if blockers and not args.dry_run:
        for b in blockers:
            print(f"refusing to record timings ({args.host_class}): {b}", file=sys.stderr)
        print("(pass --dry-run to exercise the harness without recording valid timings)", file=sys.stderr)
        sys.exit(2)
    for b in blockers:
        log(f"DRY RUN: ignoring host check: {b}")
    smoke = hostinfo.is_smoke_test(facts)
    if smoke:
        log("SPOT INSTANCE: results are a SMOKE TEST and will be watermarked as not for publication")
    cpuset, source = pick_cpuset(args.cpuset, cfg["container"]["cores"], args.dry_run)
    return HostContext(host_class=args.host_class, dry_run=args.dry_run, smoke_test=smoke,
                       drop=bool(facts.get("can_drop_caches")) and want_drop, facts=facts, blockers=blockers,
                       cpuset=cpuset, cpuset_source=source, cpus=hostinfo.parse_cpulist(cpuset))


def pick_cpuset(explicit: str | None, cores: int, dry_run: bool) -> tuple[str | None, str]:
    if explicit:
        return explicit, "--cpuset"
    if platform.system() != "Linux":
        return None, "none (non-Linux dry run)"
    out = subprocess.run(["bash", str(REPO / "docker" / "pick-cores.sh"), str(cores)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        if dry_run:
            return None, f"none (pick-cores failed: {out.stderr.strip()})"
        sys.exit(f"pick-cores.sh failed: {out.stderr.strip()}")
    return out.stdout.strip(), "docker/pick-cores.sh"


def dataset_info(path: Path) -> dict:
    files = sorted(path.glob("*.parquet"))
    if not files:
        sys.exit(f"no .parquet files in {path}")
    desc = path / "datagen.json"
    return {
        "path": str(path),
        "datagen": json.loads(desc.read_text()) if desc.exists() else None,
        "files": len(files),
        "bytes": sum(f.stat().st_size for f in files),
    }


def image_toolchains(cfg: dict) -> dict:
    """Toolchain versions read from INSIDE the benchmark images (not from the host)."""
    out: dict = {}
    scala = cfg["images"].get("scala")
    if scala:
        res = subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "java", scala,
                              "-XshowSettings:properties", "-version"], capture_output=True, text=True)
        text = res.stderr  # the JVM prints settings and -version to stderr
        props = dict(m.groups() for m in re.finditer(r"^\s+(java\.[\w.]+) = (.*)$", text, re.M))
        out["scala"] = {
            "image": scala,
            "java.runtime.version": props.get("java.runtime.version"),
            "java.vendor.version": props.get("java.vendor.version"),
            "java.vm.version": props.get("java.vm.version"),
            "java_version_output": "\n".join(l for l in text.splitlines() if not l.startswith((" ", "\t"))
                                             and l.strip() and not l.startswith("Property settings")),
        }
    rust = cfg["images"].get("rust")
    if rust:
        res = subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "cat", rust,
                              "/usr/local/share/rvj/toolchain.txt"], capture_output=True, text=True)
        lines = res.stdout.splitlines()
        out["rust"] = {
            "image": rust,
            "rustc_version": next((l for l in lines if l.startswith("rustc ")), None),
            "cargo_version": next((l for l in lines if l.startswith("cargo ")), None),
            "rustc_vV": res.stdout if res.returncode == 0 else None,
            "error": res.stderr.strip() or None if res.returncode else None,
        }
    return out


def base_manifest(cfg: dict, ctx: HostContext, stamp: str, data: dict) -> dict:
    return {
        "schema": 3,
        "dry_run": ctx.dry_run,
        "smoke_test": ctx.smoke_test,
        "host_class": ctx.host_class,
        "gates": {"blockers_ignored_by_dry_run": ctx.blockers, "facts": ctx.facts},
        "ec2": ctx.facts.get("ec2"),
        "data_storage": ctx.facts.get("data_storage"),
        "started_utc": stamp,
        "argv": sys.argv,
        "git": hostinfo.git_section(REPO),
        "host": hostinfo.host_section(),
        "docker": hostinfo.docker_section(cfg["images"]),
        "image_toolchains": image_toolchains(cfg),
        "cpuset": {"cpus": ctx.cpuset, "source": ctx.cpuset_source,
                   "thread_siblings": hostinfo.thread_siblings(ctx.cpus)},
        "drop_caches_effective": ctx.drop,
        "dataset": data,
        "config": cfg,
        "clock_start": hostinfo.clock_state(),
    }


def close_manifest(manifest: dict) -> None:
    """Record finish time and end-of-run clock state (pair with `clock_start`)."""
    manifest["finished_utc"] = utc_stamp()
    end = hostinfo.clock_state()
    manifest["clock_end"] = end
    start = manifest.get("clock_start") or {}
    if "realtime_minus_monotonic_ns" in start:
        # ~0 unless the realtime clock was stepped (or slewed) during the experiment.
        manifest["clock_realtime_drift_ns"] = end["realtime_minus_monotonic_ns"] - start["realtime_minus_monotonic_ns"]


# --------------------------------------------------------------------------- commands

def pipeline_args(cfg: dict) -> list[str]:
    p = cfg["pipeline"]
    return ["--input", "/data", "--output", "/out/output.parquet", "--summary", "/out/summary.json",
            "--batch-log", "/out/batches.csv", "--workers", str(p["workers"]), "--batch-size", str(p["batch_size"])]


def rust_command(cfg: dict, conf: dict) -> list[str]:
    return [*conf["command"], *pipeline_args(cfg)]


def jvm_command(cfg: dict, heap_flags: list[str], gc_flags: list[str], *, jfr: bool = True,
                nmt: bool = False) -> list[str]:
    flags = [*heap_flags, *gc_flags]
    if jfr:
        flags += [f"-XX:StartFlightRecording=filename=/out/gc.jfr,settings={JFC},dumponexit=true",
                  "-Xlog:gc*:file=/out/gc.log:uptime,level,tags"]
    if nmt:
        flags += ["-XX:NativeMemoryTracking=summary", "-XX:+UnlockDiagnosticVMOptions", "-XX:+PrintNMTStatistics"]
    return ["java", *flags, "-jar", JAR, *pipeline_args(cfg)]


# --------------------------------------------------------------------------- one container run

def read_kv(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    return {k: int(v) for k, _, v in (l.partition(" ") for l in path.read_text().splitlines()) if v.isdigit()}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def parse_nmt(stdout: Path, run_dir: Path) -> dict:
    text = stdout.read_text() if stdout.exists() else ""
    start = text.find("Native Memory Tracking:")
    if start < 0:
        return {}
    block = text[start:]
    (run_dir / "nmt.txt").write_text(block)
    # JDK 21 prints bytes at exit (no unit); other scales print a KB/MB/GB suffix.
    units = {"": 1, "B": 1, "KB": 2**10, "MB": 2**20, "GB": 2**30}

    def committed(pattern: str) -> int | None:
        m = re.search(pattern + r"reserved=\d+(?:[KMG]?B)?, committed=(\d+)([KMG]?B)?", block)
        return int(m.group(1)) * units[m.group(2) or ""] if m else None

    return {"nmt_total_committed_bytes": committed(r"Total: "),
            "nmt_heap_committed_bytes": committed(r"Java Heap \(")}


def uptime_ns(wrapper: dict) -> int | None:
    """Wall time from the wrapper's /proc/uptime readings (decimal seconds, 10 ms resolution)."""
    try:
        whole = [s.split(".") for s in (wrapper["start_uptime_s"], wrapper["end_uptime_s"])]
        (s0, f0), (s1, f1) = [(int(a), int(b.ljust(9, "0")[:9])) for a, b in whole]
    except (KeyError, ValueError, TypeError):
        return None
    return (s1 - s0) * 1_000_000_000 + (f1 - f0)


def execute(cfg: dict, ctx: HostContext, *, image: str, command: list[str], run_dir: Path, data: Path,
            timeout_s: float | None = None) -> dict:
    """Run one container under the resource envelope; return the measurement row."""
    c = cfg["container"]
    dry_run = ctx.dry_run
    run_dir.mkdir(parents=True)
    os.chmod(run_dir, 0o777)  # the container may run as a different uid

    create = ["create", "--name", f"rvj-{run_dir.parent.parent.name}-{run_dir.name}"[:120], "--network", "none",
              "--cpus", str(c["cpus"]), "--memory", c["memory"], "--memory-swap", c["memory_swap"],
              "-v", f"{data.resolve()}:/data:ro", "-v", f"{run_dir.resolve()}:/out"]
    if ctx.cpuset:
        create += ["--cpuset-cpus", ctx.cpuset]
    if hasattr(os, "getuid"):
        create += ["--user", f"{os.getuid()}:{os.getgid()}"]
    create += [image, *command]

    if ctx.drop:
        hostinfo.drop_caches()
    cid = docker(*create)
    timed_out = False
    smp = None
    try:
        steal0 = hostinfo.steal_ticks(ctx.cpus or None)  # steal on the run's cores (all CPUs if unpinned)
        t0 = time.monotonic_ns()
        docker("start", cid)
        pid = int(docker("inspect", "--format", "{{.State.Pid}}", cid) or 0)
        cgroup = container_cgroup(pid, cid) if pid else None
        if cgroup:
            smp = CgroupSampler(cgroup, cfg["protocol"]["sample_hz"], t0, cpus=ctx.cpus or None)
            smp.start()
        elif not dry_run:
            log(f"WARNING: could not locate cgroup for {run_dir.name}; no samples for this run")
        waiter = subprocess.Popen(["docker", "wait", cid], stdout=subprocess.PIPE, text=True)
        try:
            out, _ = waiter.communicate(timeout=timeout_s)
            exit_code = int(out.strip())
        except subprocess.TimeoutExpired:
            timed_out = True
            docker("kill", cid, check=False)
            out, _ = waiter.communicate()
            exit_code = int(out.strip() or -1)
        host_wall_ns = time.monotonic_ns() - t0
        steal1 = hostinfo.steal_ticks(ctx.cpus or None)
        if smp:
            smp.stop()
            smp.write_csv(run_dir / "cgroup_samples.csv")
        oom = docker("inspect", "--format", "{{.State.OOMKilled}}", cid) == "true"
    finally:
        docker("rm", "-f", cid, check=False)

    wrapper = read_json(run_dir / "wrapper.json")
    summary = read_json(run_dir / "summary.json")
    if not wrapper and exit_code == 0:
        # The container never wrote to the run directory (e.g. a path the Docker
        # daemon cannot see). Never let such a run count as a success.
        log(f"ERROR: {run_dir.name}: no wrapper.json in {run_dir}; marking the run as failed")
        exit_code = -2
    cpu = read_kv(run_dir / "cgroup_final" / "cpu.stat")
    mem_peak = run_dir / "cgroup_final" / "memory.peak"
    stderr = run_dir / "stderr.txt"
    row = {
        "run_id": run_dir.name, "dry_run": dry_run, "exit_code": exit_code, "oom_killed": oom,
        "java_oom": stderr.exists() and "OutOfMemoryError" in stderr.read_text(errors="replace"),
        "timed_out": timed_out,
        # Primary wall time from /proc/uptime (immune to NTP/VM clock steps, 10 ms
        # resolution); the realtime-clock difference is kept for reference.
        "wall_ns": uptime_ns(wrapper),
        "wall_realtime_ns": wrapper["end_ns"] - wrapper["start_ns"] if wrapper else None,
        "host_wall_ns": host_wall_ns,
        "pipeline_wall_ns": summary.get("wall_nanos"),
        "cpu_usage_usec": cpu.get("usage_usec"), "cpu_user_usec": cpu.get("user_usec"),
        "cpu_system_usec": cpu.get("system_usec"), "cpu_throttled_usec": cpu.get("throttled_usec"),
        # M_peak: private memory = anon + shmem. ZGC backs its heap with memfd
        # shared memory, which cgroup v2 accounts as shmem (inside `file`), not anon.
        "peak_private_bytes": smp.peak_of(lambda r: r["anon"] + r["shmem"]) if smp else None,
        "peak_anon_bytes": smp.peak("anon") if smp else None,
        "peak_shmem_bytes": smp.peak("shmem") if smp else None,
        "peak_pagecache_bytes": smp.peak_of(lambda r: r["file"] - r["shmem"]) if smp else None,
        "peak_file_bytes": smp.peak("file") if smp else None,
        "peak_kernel_bytes": smp.peak("kernel") if smp else None,
        "peak_current_bytes": smp.peak("memory_current") if smp else None,
        "memory_peak_bytes": int(mem_peak.read_text().split()[0]) if mem_peak.exists() else None,
        "samples": len(smp.rows) if smp else 0,
        "rows_read": summary.get("rows_read"), "rows_kept": summary.get("rows_kept"),
        "groups": summary.get("groups"),
    }
    # CPU steal on the run's cores over [docker start, docker wait], in USER_HZ ticks.
    steal_usec = hostinfo.ticks_to_usec(steal1 - steal0) if steal0 is not None and steal1 is not None else None
    cpu_usec = row["cpu_usage_usec"]
    row["steal_usec"] = steal_usec
    row["steal_fraction"] = steal_usec / cpu_usec if steal_usec is not None and cpu_usec else None
    row["steal_flagged"] = bool(row["steal_fraction"] is not None
                                and row["steal_fraction"] > cfg["protocol"]["steal_flag_fraction"])
    if smp:
        mhz = [r["cpu_mhz"] for r in smp.rows if r.get("cpu_mhz") is not None]
        if mhz:
            row.update({"cpu_mhz_mean": sum(mhz) / len(mhz), "cpu_mhz_min": min(mhz), "cpu_mhz_max": max(mhz)})
    row.update(parse_nmt(run_dir / "stdout.txt", run_dir))
    return row


def status_line(row: dict) -> str:
    if row["exit_code"] == 0:
        status = "ok"
    else:
        status = f"exit {row['exit_code']}" + (" java-OOM" if row.get("java_oom") else "") \
            + (" cgroup-OOM" if row.get("oom_killed") else "") + (" TIMEOUT" if row.get("timed_out") else "")
    steal = row.get("steal_fraction")
    return (f"{status}; wall {(row['wall_ns'] or 0) / 1e9:.2f}s; cpu {(row['cpu_usage_usec'] or 0) / 1e6:.2f}s; "
            f"peak anon+shmem {(row['peak_private_bytes'] or 0) / 2**20:.0f} MiB; samples {row['samples']}"
            + (f"; steal {steal * 100:.2f}%" + (" FLAGGED" if row.get("steal_flagged") else "")
               if steal is not None else ""))


# --------------------------------------------------------------------------- post-processing

def parse_jfr(cfg: dict, run_dir: Path) -> dict:
    if not (run_dir / "gc.jfr").exists():
        return {}
    res = subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "jfr",
                          "-v", f"{run_dir.resolve()}:/out", cfg["images"]["scala"],
                          "print", "--json", "--events", jfrparse.EVENTS, "/out/gc.jfr"],
                         capture_output=True, text=True)
    if res.returncode != 0:  # e.g. a recording truncated by a killed run
        log(f"WARNING: jfr print failed for {run_dir.name}: {res.stderr.strip()[:200]}")
        return {}
    (run_dir / "gc.json").write_text(res.stdout)
    return jfrparse.convert(run_dir / "gc.json", run_dir)


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def check_outputs(rows: list[dict], runs_dir: Path) -> dict:
    paths = [runs_dir / r["run_id"] / "output.parquet" for r in rows
             if r.get("kind") != "warmup" and r["exit_code"] == 0]
    paths = [p for p in paths if p.exists()]
    if len(paths) < 2:
        return {"compared": len(paths), "identical": None, "errors": []}
    errors = verify.compare_outputs(paths)
    return {"compared": len(paths), "identical": not errors, "errors": errors[:20]}
