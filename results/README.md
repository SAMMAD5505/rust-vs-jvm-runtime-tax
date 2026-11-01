# results/

Each `bench/run.py` invocation writes `results/<UTC timestamp>/`. The suffix
is `-dryrun` for dry runs and `-smoke` for Spot-instance smoke tests; both are
watermarked by the analysis and must not be published. Run directories are
git-ignored and are archived alongside the paper instead
(`make export-results` → `results/exports/*.tar.gz` + `.sha256`).

`manifest.json` (schema 3) records:
- `host_class`, `smoke_test`, `ec2` (IMDSv2: instance type and id, region,
  AZ, AMI, lifecycle) and `data_storage` (device, filesystem, disk models)
- `gates`: the facts and any blockers a dry run ignored
- `cpuset.thread_siblings`
- `image_toolchains`: read from inside the images at bench start, not from
  the host:
  - `scala`: `java.runtime.version`, `java.vendor.version` and the
    `java -version` output
  - `rust`: `rustc_version` and `cargo_version` from
    `/usr/local/share/rvj/toolchain.txt`, which the image build writes after
    asserting rustc/cargo 1.81.0

  Both image builds fail unless the toolchain is exactly the pinned one
  (Temurin-21.0.4+7; rustc/cargo 1.81.0).
- `clock_start` / `clock_end` / `clock_realtime_drift_ns`

`make compare-hosts` writes `results/compare-<vm>-vs-<metal>/`:
`compare_hosts.{csv,json}`, `table_compare_hosts.tex`, `fig_compare_hosts.{pdf,png}`.

```
results/<timestamp>/
├── manifest.json        # host, toolchain, image digests, flags, protocol, schedule, output check
├── runs.csv             # one row per run (measured, warmup, nmt)
├── runs/<order>-<config>-<kind>/
│   ├── summary.json         # pipeline summary (rows, groups, wall_nanos)
│   ├── batches.csv          # batch_id,worker,start_nanos,rows,nanos,setup_nanos
│   ├── cgroup_samples.csv   # 10 Hz host samples: memory.current, memory.peak, memory.stat keys, cpu usage
│   ├── cgroup_final/        # in-container snapshot at exit: cpu.stat, memory.stat, memory.peak, memory.events, ...
│   ├── wrapper.json         # in-container start/end timestamps (ns) and exit code
│   ├── output.parquet       # aggregates (checked bit-identical across runs)
│   ├── stdout.txt, stderr.txt
│   └── (JVM) gc.jfr, gc.json, gc.log, gc_pauses.csv, gc_collections.csv, heap.csv, nmt.txt (NMT runs)
└── analysis/            # written by analysis/analyze.py: stats.json, summary_*.csv, fig_*, table_*.tex
```

### Heap sweep (`bench/heapsweep.py`)

`results/<timestamp>-heapsweep[-dryrun]/` has the same per-run layout plus
`hmin.json`. For each collector it records:
- every probe: heap, pass/fail, attempts run and completed, failure reasons
- H_min, the baseline wall time and the probe timeout
- `reserve`: the NMT run, measured non-heap committed MiB, reserve MiB and heap cap MiB

`runs.csv` holds NMT reserve runs (`kind=nmt`), probe runs (`kind=probe`),
warm-ups and measured runs. All of them use the same JVM flags, with JFR on.
It has three extra columns:
- `heap_mib`
- `multiple` (× H_min; empty for NMT runs, probes and Rust references)
- `attempt` (probes only)

`manifest.json` adds `hmin_search`, `sweep_points`, `skipped_points` (each
with a `reason`), `heap_caps_mib` and `sweep_schedule`.
`analysis/heapsweep.py` writes `analysis/`.

`results/<timestamp>-heapsweep-validate[-dryrun]/` holds the probes on another
preset at H_min and ⌈1.25 × H_min⌉, with all attempts run. `validation.json`
records attempts completed/run per probe and `agrees` per collector, and the
manifest adds `validate_from` and `row_scale`.

`results/<timestamp>-calibrate[-dryrun]/` holds one rust-system and one
scala-g1 run. `calibration.json` has the measured per-run times and the
projected totals.

## Memory metrics

| metric | definition |
|---|---|
| **M_peak** (`peak_private_bytes`) | max over 10 Hz samples of `memory.stat anon + shmem`. `shmem` is included because ZGC backs the Java heap with memfd shared memory, which cgroup v2 accounts as `shmem` (inside `file`), not `anon`. |
| `peak_anon_bytes`, `peak_shmem_bytes` | the two components, separately |
| `peak_pagecache_bytes` | max of `file − shmem` (page cache charged to the container) |
| `peak_current_bytes` | max sampled `memory.current` (total charged memory) |
| `memory_peak_bytes` | cgroup `memory.peak` at exit (includes page cache) |

## runs.csv columns

| column | meaning |
|---|---|
| `run_id` | `<order>-<config>-<kind>` |
| `config`, `kind` | configuration from `bench/configs.yaml`; `measured`, `warmup` (discarded) or `nmt` (memory accounting only, excluded from timing) |
| `order`, `preset`, `dry_run` | schedule position, dataset preset, dry-run flag |
| `exit_code`, `oom_killed`, `java_oom`, `timed_out` | container outcome; `java_oom` = `OutOfMemoryError` in stderr; `timed_out` = killed by the harness timeout |
| `wall_ns` | in-container wall time of the pipeline process (includes JVM startup), from `/proc/uptime` (not stepped by NTP/VM clock resyncs; 10 ms resolution) |
| `wall_realtime_ns` | the same interval from the realtime clock (`date +%s%N`), for reference only; can be distorted by clock steps |
| `host_wall_ns` | `docker start` → `docker wait` on the host |
| `pipeline_wall_ns` | wall time reported by the pipeline itself (main → output fsync) |
| `cpu_usage_usec`, `cpu_user_usec`, `cpu_system_usec`, `cpu_throttled_usec` | cgroup `cpu.stat` at exit |
| `peak_*_bytes`, `memory_peak_bytes`, `samples` | see *Memory metrics* |
| `rows_read`, `rows_kept`, `groups` | from `summary.json` |
| `gc_count`, `gc_pause_count`, `gc_pause_total_ns`, `gc_pause_max_ns`, `gc_duration_total_ns`, `z_alloc_stall_total_ns` | from JFR (`jdk.GarbageCollection`, `jdk.GCPhasePause`, `jdk.ZAllocationStall`); `gc_duration_total_ns` = Σ GC-cycle durations (wall, incl. concurrent phases) |
| `heap_used_max`, `heap_committed_max` | from JFR `jdk.GCHeapSummary` |
| `jdk_version` | JVM runs: runtime version from JFR `jdk.JVMInformation` (e.g. `21.0.4+7-LTS`). The analysis exits if it differs across runs or from `image_toolchains.scala.java.runtime.version`. |
| `nmt_total_committed_bytes`, `nmt_heap_committed_bytes` | NMT runs only |
| `steal_usec`, `steal_fraction`, `steal_flagged` | CPU steal on the pinned cores over the run (`/proc/stat`); fraction = steal ÷ run CPU time; flagged above `protocol.steal_flag_fraction` (0.5%) |
| `cpu_mhz_mean`, `cpu_mhz_min`, `cpu_mhz_max` | observed MHz of the pinned cores from 10 Hz `/proc/cpuinfo` samples |

`cgroup_samples.csv` also carries per-sample `steal_ticks` (cumulative, USER_HZ)
and `cpu_mhz`.
