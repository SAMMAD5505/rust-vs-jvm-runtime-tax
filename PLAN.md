# PLAN — rust-vs-jvm-runtime-tax

Reproducible artifact for *"Quantifying the Runtime Tax: Memory Inflation, CPU
Efficiency, and Cloud Infrastructure Costs of Native Rust vs. JVM Scala in Big
Data Platforms"* (Sowdari & Sowdari).

This document fixes the design **before** any measurement is taken. No
benchmark numbers appear anywhere in this repository except in `results/`
files produced by the harness.

---

## 1. Pinned toolchain and dependencies

| Component | Version | Notes |
|---|---|---|
| rustc / cargo | **1.81.0** | as stated in the paper (§II-B) |
| `parquet` crate | **=53.4.1** (`arrow` feature) | builds on rustc 1.81; 53.3.0 could not be pinned consistently because `arrow 53.3.0` pulls `arrow-* ^53.3.0`, which resolved to 53.4.1 |
| `arrow` crate | **=53.4.1** (default features off) | all 13 arrow/parquet crates locked to 53.4.1 |
| `rand` / `rand_chacha` / `rand_distr` | =0.8.5 / =0.3.1 / =0.4.3 | datagen only; stable value streams |
| `mimalloc` crate | =0.1.43 | optional feature of pipeline-rust |
| Scala | **3.5.0** | as stated in the paper |
| JDK | **Temurin 21.0.4+7** | as stated in the paper |
| sbt / sbt-assembly | 1.10.2 / 2.2.0 | |
| parquet-java (`parquet-hadoop`) | **1.14.4** | `LocalInputFile` / `LocalOutputFile` |
| Hadoop | `hadoop-client-api` + `hadoop-client-runtime` **3.3.6** | shaded clients; needed only for `Configuration` / codec classes |
| slf4j-nop | 2.0.16 | silences parquet-java logging |
| Python (verify/bench/analysis) | 3.12, pyarrow =17.0.0 | |

Transitive Rust dependencies are pinned in `Cargo.lock` (committed). The lock
file is resolved with `incompatible-rust-versions = "fallback"` so every crate
builds on rustc 1.81.0. Docker base images are pinned by digest in
`docker/*.Dockerfile`.

## 2. Dataset (datagen/)

Schema (all columns `REQUIRED`):

| column | Arrow type | Parquet physical type | generation |
|---|---|---|---|
| `id` | FixedSizeBinary(16) | FIXED_LEN_BYTE_ARRAY(16) | UUIDv4 (random bytes, version/variant bits set) |
| `ts` | Int64 | INT64 | epoch-ms, uniform over 30 days from 2024-01-01T00:00:00Z |
| `payload` | Utf8 | BYTE_ARRAY (UTF8) | exactly 1024 chars from `[A-Za-z0-9_-]` |
| `metric` | Float64 | DOUBLE | Normal(100, 25), clamped to ≥ 0 |

Parquet writer settings: `UNCOMPRESSED`, dictionary **disabled**, `PLAIN`
encoding, data page v1. These settings plus seed, row count and generator
version are written to the file key/value metadata (`rvj.*` keys).

Determinism: file *i* uses `ChaCha8Rng::seed_from_u64(seed)` with stream *i*,
so output is byte-identical for a given (seed, rows, files, row-group-size,
container image) regardless of thread count.

**Row-group size is 65,536 rows (~69 MB) to stay memory-bounded under the
4 GB cgroup: 4 workers × one in-memory row group ≈ 280 MB.**

Row-group size default: **65,536 rows (~69 MB)**, *not* 1,048,576.
A 1 Mi-row group is ~1.1 GB; parquet-java materializes a whole row group in
memory, so 4 workers would need ~4.4 GB against a 3 GB heap and the Scala
pipeline could not run inside the 4 GB envelope at all.

Presets (≈1,060 bytes/row on disk):

| preset | rows | files | approx. size |
|---|---|---|---|
| dev | 1,000,000 | 4 | ~1.06 GB |
| small | 10,000,000 | 8 | ~10.6 GB |
| full | 950,000,000 | 64 | ~1.0 TB (0.92 TiB) — requires `--yes` |

> **Paper inconsistency to fix:** §II-C says 1.2 billion rows ≈ 1 TB. At
> 1,060 B/row, 1.2 B rows ≈ 1.27 TB. Either state 950 M rows / 1.0 TB or
> 1.2 B rows / 1.27 TB.

## 3. Workload (identical in both languages)

Work unit = one (file, row group). Units are numbered in (file name, row-group
index) order; 4 workers pull units from a shared atomic counter.

Within a unit, rows are processed in **batches of 8,192 rows**:

1. **Materialize** each row into a language-native owned record
   (`Row { id: [u8;16], ts: i64, payload: String, metric: f64 }` in Rust;
   `case class Row(id: Array[Byte], ts: Long, payload: String, metric: Double)`
   in Scala). The batch is first collected into a `Vec<Row>` / `Array[Row]`.
   Rust owns its `String` (heap allocation + copy per row), matching the JVM's
   per-row `String`.
2. **Filter** `metric > 75.0`.
3. **Transform** into `Enriched`:
   - `hour_bucket = ts / 3_600_000`
   - `payload_hash = FNV-1a-64(payload bytes)`
   - `tag = first 8 chars of payload, ASCII-uppercased` (owned string)
4. **Aggregate** by `hour_bucket`: `count`, `sum(metric)`, `min(metric)`,
   `max(metric)`, `checksum = XOR of row_hash` where
   `row_hash = payload_hash ^ FNV-1a-64(tag) ^ id_lo ^ id_hi`
   (`id_lo`/`id_hi` = little-endian u64 halves of `id`).
   Folding `tag` and `id` into the checksum guarantees neither LLVM nor the
   JIT can dead-code-eliminate that work.
5. **Write** aggregates sorted by `hour_bucket` to one Parquet file
   (`UNCOMPRESSED`, no dictionary), then `fsync` the file.

Output schema: `hour_bucket INT64, count INT64, sum_metric DOUBLE,
min_metric DOUBLE, max_metric DOUBLE, checksum INT64` (u64 bits stored as
signed).

### Determinism of floating point

Each unit keeps its own partial aggregates; `sum` is accumulated in row order
within the unit. After all workers finish, partials are merged **in unit
order** (sum via sequential `+=`). Both languages use IEEE-754 binary64 with no
reassociation (Rust never reassociates; Java is strict-FP since 17), so the
two outputs are bit-identical and reproducible across runs.

`make verify` decodes both output files and compares every value bit-for-bit
(file bytes differ because of writer `created_by` metadata). It also compares
`rows_read` / `rows_kept` from the run summaries.

### Instrumentation (same format in both)

- `batches.csv`: `batch_id,worker,start_nanos,rows,nanos,setup_nanos`
  - `batch_id` = `<file_idx>-<row_group>-<batch_idx>`
  - `nanos` = time to process one 8,192-row record batch: decode +
    materialize rows + filter + transform + aggregate.
  - `setup_nanos` (non-zero only on batch 0 of each unit) = opening the file,
    reading the footer and preparing the row-group reader.
  - **PRIMARY latency metric = per-unit latency = `setup_nanos` + Σ `nanos`
    over one full 65,536-row row-group unit.** The scope is identical in both
    languages regardless of where each library does its I/O. Each file's last,
    shorter row group is excluded.
  - **SECONDARY metric = per-batch latency (`nanos`)**, with a known
    asymmetry: parquet-java's `readRowGroup` reads the whole row group from
    disk during setup, whereas arrow-rs reads pages lazily, so Rust's page I/O
    falls inside `nanos` and Scala's inside `setup_nanos`.
  - Both are reported as p50/p95/p99/p99.9 with cluster-bootstrap CIs
    (resampling whole runs).
  - `start_nanos` is relative to process start (for correlating with GC).
  - Rows are buffered in memory and written after processing ends.
- `summary.json`: `lang, workers, batch_size, files, units, rows_read,
  rows_kept, groups, wall_nanos`. Wall time runs from process start to after
  the output `fsync`.

## 4. Rust pipeline (pipeline-rust/)

- Cargo workspace at repo root (members `datagen`, `pipeline-rust`); release
  profile `opt-level=3, lto="fat", codegen-units=1, panic="abort"`.
- Reader: `ParquetRecordBatchReaderBuilder` restricted to one row group,
  `batch_size = 8192`.
- Allocator: feature `system` (default) or `mimalloc`
  (`--no-default-features --features mimalloc`).
- Threads: `std::thread::scope`, 4 workers, no rayon.
- Group map: `std::collections::HashMap<i64, Agg>`.

## 5. Scala pipeline (pipeline-scala/)

- Plain JVM app, no Spark. Fat jar via sbt-assembly.
- Reader: `ParquetFileReader.open(LocalInputFile, ParquetReadOptions)`,
  `readRowGroup(i)`, and a custom `RecordMaterializer[Row]` via
  `MessageColumnIO.getRecordReader` (no intermediate `Group` objects).
- Threads: `Executors.newFixedThreadPool(4)`, 4 tasks pulling from an
  `AtomicInteger`.
- Group map: `scala.collection.mutable.LongMap[Agg]` (unboxed keys).
- Writer: `ExampleParquetWriter` on `LocalOutputFile`, then
  `FileChannel.force(true)`.

## 6. Docker and isolation (docker/)

- `docker/rust.Dockerfile`, `docker/scala.Dockerfile`: multi-stage builds,
  base images pinned by digest, sbt tarball verified by SHA-256. Runtime
  entrypoint `docker/run-wrapper.sh` runs the pipeline and, before exiting,
  snapshots the container's own cgroup files (`cpu.stat`, `memory.*`) — the
  host-side cgroup directory disappears when the container stops.
- Run flags: `--cpus=4 --memory=4g --memory-swap=4g --cpuset-cpus=<cores>
  --network none --user <uid>:<gid>`; data mounted read-only, per-run output
  directory mounted read-write.
- `docker/pick-cores.sh N` picks N logical CPUs on N distinct physical cores
  (one NUMA node, skipping core 0) so that SMT siblings stay unassigned.

## 7. Benchmark harness (bench/)

`bench/run.py` + `bench/configs.yaml`. Configurations: `rust-system`,
`rust-mimalloc`, `scala-g1`, `scala-zgc` (`-XX:+UseZGC -XX:+ZGenerational`),
`scala-parallel`; all JVM runs `-Xms3G -Xmx3G -XX:+AlwaysPreTouch`, JFR with
`profiling/gc.jfc`, `-Xlog:gc*`.

**Hosts (changed 2026-10-31).** Primary: AWS EC2 `c6id.4xlarge` On-Demand
(8 physical cores × 2 SMT, Xeon Platinum 8375C, 32 GiB, 950 GB NVMe instance
store, Ubuntu 24.04). Validation: `c6id.metal`, small preset only. The
laptop/hybrid-CPU route is dropped. Operational guide: `docs/aws.md`.

**Host gates (`--host-class`; `bench/hostinfo.py: evaluate_gates`, unit-tested
in `bench/tests/test_hostinfo.py`).**
- **Both classes:** Linux, `cgroup2fs`, and the page cache can be dropped
  (root, or the sudoers rule for `/usr/sbin/sysctl vm.drop_caches=3` that
  `scripts/ec2-setup.sh` installs). On EC2, a Spot instance is refused unless
  `--allow-spot`; such results are suffixed `-smoke` and watermarked SMOKE
  TEST.
- **`bare-metal`:** no `hypervisor` CPU flag. `c6id.metal` passes this gate
  unchanged and is recorded as `host_class=bare-metal` with its EC2 identity.
- **`cloud-vm`:** EC2 detected via IMDSv2, instance type in the allowlist
  (`c6id.4xlarge`, `c6id.metal`), and the data directory on "Amazon EC2 NVMe
  Instance Storage". The storage check follows partitions, dm and md down to
  the disk model; EBS and mixed devices are refused.

`--dry-run` bypasses the gates; such results are suffixed `-dryrun`, flagged
in `manifest.json` and `runs.csv`, and the analysis refuses them unless
`--allow-dry-run` (then every figure and table is watermarked).
`make aws-preflight` reports every gate for both classes without running
anything.

**VM noise controls.** Per run, CPU steal on the pinned cores (`/proc/stat`,
10 Hz series plus start/end totals) and observed MHz (`/proc/cpuinfo`).
Runs with steal > `protocol.steal_flag_fraction` (0.5%) of their CPU time are
flagged. The analysis reports flagged counts per configuration and a
sensitivity table excluding them. N = `hosts.cloud_vm.runs` (20) per
configuration on `cloud-vm`, versus 10 on `bare-metal`. The heap sweep's
per-point N follows the same default. The coefficient of variation is in
`table_runtime`. Governor and turbo are recorded as "unavailable" when not
exposed.

**VM vs metal.** `make compare-hosts` takes the c6id.4xlarge and c6id.metal
small-preset results. For each configuration it reports the ratio to
`rust-system` (CPU-seconds, M_peak, p99 per-unit latency) on both hosts, with
95% bootstrap CIs (runs resampled), plus the VM/metal ratio-of-ratios.

**Memory (10 Hz host sampler, `bench/sampler.py`).** Each sample records
`memory.current`, `memory.peak`, `memory.stat` (`anon`, `file`, `kernel`,
`shmem`, `sock`, …) and `cpu.stat usage_usec`.
**M_peak = max(anon + shmem)**, not `anon` alone: ZGC backs the Java heap with
memfd shared memory, which cgroup v2 accounts as `shmem` (inside `file`).
Found in the first dry run — `anon` showed ≈130 MiB for ZGC against a 3 GB
committed heap (NMT confirmed ≈3.2 GB committed). Page cache = `file − shmem`,
reported separately with `memory.current` and `memory.peak`.

**JVM memory detail.** JFR `jdk.GCHeapSummary` gives heap used/committed per
GC. One extra run per JVM config adds `-XX:NativeMemoryTracking=summary
-XX:+PrintNMTStatistics`; these `nmt` runs are excluded from timing stats.

**Production-style vs heap sweep.** The fixed `-Xms3G -Xmx3G
-XX:+AlwaysPreTouch` matrix above is the *production-style* configuration
(capacity-planned service); its JVM M_peak reflects the setting. The heap
sweep measures the need.

**Heap sweep (`bench/heapsweep.py`, `make heapsweep`; Hertz & Berger,
OOPSLA 2005).** Default dataset: small preset.
0. *Off-heap reserve, measured* per collector: one NMT run at 3,072 MiB on
   the sweep dataset; non-heap = NMT total committed − Java heap committed;
   reserve_c = max(512, ⌈1.5 × non-heap / 64⌉ × 64) MiB; heap cap_c =
   4,096 − reserve_c. Measured value, reserve and cap are recorded in
   `hmin.json`, the manifest and the H_min table.
1. *H_min search* per collector (G1, generational ZGC, Parallel): probe the
   upper bound (3072 MiB) first — its wall time sets the probe timeout
   (max(120 s, 10 × wall)) — then 128 MiB, then bisect at 128 MiB
   granularity keeping "lo fails, hi passes". A probe runs up to 3 attempts
   with **exactly the sweep's JVM flags** (`-Xms=-Xmx`, no pre-touch, JFR
   and GC logging on); it passes only if all 3 exit 0 (a Java OOME, cgroup
   OOM kill, other non-zero exit or timeout fails it, and stops the
   remaining attempts). H_min = smallest passing heap.
2. *Sweep* each collector at 1.25×, 1.5×, 2×, 3×, 4× its own H_min
   (rounded up to 1 MiB), skipping points above cap_c (recorded with the
   reason). Failed measured runs are counted, never retried; tables show
   completed/total per point (e.g. "9/10").
3. 1 warm-up per point first, then N = 10 measured runs per point plus 10 per
   Rust reference (system, mimalloc), interleaved in seeded random order.
4. Recorded per run: CPU-seconds, wall, M_peak, GC pause percentiles, total
   pause time (Σ `jdk.GCPhasePause`), total GC-cycle time
   (Σ `jdk.GarbageCollection` durations), plus the usual columns.
5. `analysis/heapsweep.py`: CPU-seconds vs heap multiple and M_peak vs heap
   multiple (one line per collector, Rust as horizontal lines), H_min table,
   per-point table. Dry-run watermark logic as in `analyze.py`.

**Protocol.** Page cache dropped before every run. 2 JVM warm-up runs per JVM
config on the warm-up dataset (discarded). N = 10 measured runs per config in
an order shuffled with a recorded seed. 1 NMT run per JVM config at the end.
After all runs: JFR parsed with `jfr print --json` (`bench/jfrparse.py`), and
every run's `output.parquet` is checked bit-identical.

**Preset plan for the paper.**

| Experiment | Preset | Target |
|---|---|---|
| Calibration (`make calibrate`) | small | 1 rust-system + 1 scala-g1 (3 GiB) run; prints projected totals for bench small/medium, heap sweep and validation. Run first on the host; starts nothing long. |
| Heap sweep (`make heapsweep`) | **small** (10 M rows) | H_min, reserves, CPU/M_peak vs heap multiple |
| H_min validation (`make heapsweep-validate`) | **medium** (95 M rows, ≈100.7 GB) | per collector, 3 attempts each at the small-preset H_min and at ⌈1.25 × H_min⌉, all attempts run |
| Production-style comparison (`make bench`) | **medium** (95 M rows); plus small on c6id.4xlarge and c6id.metal for `make compare-hosts` | headline CPU, wall, M_peak, latency, GC, cost |

Rationale: the workload streams with bounded state (720 hourly groups,
4 workers × one row group in flight), so H_min should not depend on dataset
size. That assumption is *tested*, not assumed: validation passes only if
both probes complete 3/3 on the medium preset. Any disagreement is reported
as a finding (and the sweep's H_min must then be qualified as
small-preset-specific).

**Manifest.** git SHA/dirty/describe, host (OS, kernel, CPU model, logical
CPUs, SMT control/active, governors, cpufreq driver, boost/turbo state, THP,
kernel cmdline, hypervisor flag, systemd-detect-virt), Docker server/client
versions, cgroup driver/version, image IDs + repo digests + labels, cpuset and
how it was chosen, dataset descriptor, full resolved config and per-config
container commands, schedule + seed, output check, failed runs.

**Cost.** `analysis/cost.yaml` holds a placeholder `vcpu_hour` rate (null);
the cost table is emitted only when it is filled in. Both provisioned
(4 vCPU × wall) and consumed (`usage_usec`) vCPU-hours are reported.

## 8. Analysis (analysis/) — after verify

Mean, stddev, 95% CI (t-distribution); Mann-Whitney U + effect size
(Cliff's δ) between configurations; bootstrapped CIs for p99/p99.9 batch
latency; GC pause p50/p95/p99/max. IEEE-column (3.5 in) PDF+PNG plots and
booktabs LaTeX tables.

## 9. Threats to validity (tracked for README/paper)

1. arrow-rs and parquet-java differ in more than language: the Rust reader
   streams pages; parquet-java loads the whole row group and creates `Binary`
   slice objects per value. Part of any gap is library, not runtime.
2. JVM heap flags determine JVM memory footprint (see minimum-heap
   experiment).
3. Hash map choice differs by idiom (SipHash `HashMap` vs open-addressing
   `LongMap`).
4. Single node, 4 vCPU, no distributed shuffle: results speak to per-executor
   runtime cost, not cluster-level behaviour.
5. JIT warm-up is included in wall time for measured runs (as in production
   batch jobs); warm-up runs only warm the OS/disk, not the JVM process.

## 10. Status log

- **2026-10-23 — verify milestone reached** (development machine, Docker
  Desktop; *no performance numbers recorded*):
  - datagen dev preset: 4 files × 265,562,707 bytes; byte-identical across
    `--threads 1` and `--threads 4`.
  - Rust (system, mimalloc) and Scala (G1, ZGC generational, ParallelGC)
    outputs are bit-identical: 720 hourly groups, 1,000,000 rows read,
    841,320 kept (84.13%, consistent with P(N(100,25) > 75) = 0.8413).
  - Invariant to worker count (1/3/4) and batch size (1000/8192).
  - Verifier rejects a deliberately different output (negative control).
  - Batch logs from both languages cover the same 124 batch ids with equal
    row counts.
  - Known build note: sbt-assembly resolves 564 duplicate classpath entries
    with `MergeStrategy.first` (overlap between hadoop-client-api/runtime and
    parquet-java transitive deps); to be audited before the artifact release.

- **2026-10-24 — harness milestone** (development machine; dry runs only, *no
  performance numbers recorded*):
  - Fat-jar merge made explicit; no `.class` content conflicts (see
    `docs/fat-jar-merge.md`).
  - Small preset (10 M rows, 8 files, 160 units): all five configurations
    bit-identical (720 groups, 8,412,919 rows kept); 1,224 batch ids match
    between languages.
  - Docker images build; harness dry run on the dev preset completed 21 runs
    (warm-up, measured, NMT) with 18 outputs bit-identical, JFR parsed,
    analysis produced all figures and tables (watermarked).
  - Batch log gained `setup_nanos`; `nanos` is now strictly the per-batch
    latency.
  - Measurement bug found and fixed: M_peak changed from `anon` to
    `anon + shmem` (ZGC heap is memfd/shmem). NMT parser fixed for JDK 21's
    byte-scaled output.

- **2026-10-24 — heap-sweep milestone** (development machine; dry runs only,
  *no performance numbers recorded*):
  - Primary latency metric switched to per-unit (setup + all batches of a
    full 65,536-row unit); per-batch kept as secondary. Tail quantiles are
    suppressed below 10/(1−q) samples.
  - `bench/harness.py` factored out of `run.py`; adds per-run timeout,
    Java-OOME detection and a missing-output guard.
  - Heap sweep dry run (dev preset, 2 runs/point): H_min search completed
    for all three collectors (6 probes each, all failures detected as Java
    OOME); 15 sweep points + 2 Rust references; 70 outputs bit-identical;
    figures and tables generated (watermarked).
  - eBPF removed from the plan; measurement is cgroup v2 counters at 10 Hz.

- **2026-10-24 — pre-host milestone** (dry runs only, *no performance
  numbers recorded*):
  - H_min probes now use exactly the sweep's JVM flags (JFR on); dry-run
    search re-run and completed for all three collectors.
  - Off-heap reserve measured per collector with NMT at 3,072 MiB
    (dev dry run: non-heap 185–222 MiB → 512 MiB floor applies).
  - Point tables report completed/total ("9/10").
  - `make calibrate` and `make heapsweep-validate` added and dry-run
    (validation exercised dev → small; all collectors 3/3 at 1× and 1.25×).
  - Wall time moved from the realtime clock to `/proc/uptime` after the
    calibration dry run showed a VM clock step (realtime 12.45 s vs uptime
    10.89 s for the same run).

- **2026-10-31 — AWS milestone** (branch `cat`; dry runs only, *no
  performance numbers recorded*):
  - Host classes `bare-metal` / `cloud-vm` with IMDSv2, allowlist,
    instance-store and Spot gates; 19 unit tests, including c6id.metal
    passing as bare-metal.
  - Steal and MHz sampling, steal flagging and sensitivity table, CV in
    tables, N = 20 on cloud-vm.
  - `scripts/ec2-setup.sh`: shellcheck-clean. The instance-store logic was
    tested against loop devices in a privileged Ubuntu 24.04 container
    (`tests/ec2-setup-storage.sh`): format blank, idempotent re-run, no
    reformat of existing ext4, wiped disk → reformat + fstab replaced,
    foreign filesystem refused, root device refused.
  - `make aws-preflight`, `export-results`, `compare-hosts`; medium preset
    (95 M rows, ≈100.7 GB) added to datagen.
  - Fixed during dry runs: bind-mount sources (`/dev/sde[/subpath]`)
    misparsed as device `subpath]`.

## 11. Order of work

datagen → Rust pipeline → Scala pipeline → `verify` on dev preset
→ **stop and report** → Docker → bench → analysis → CI/README/CITATION.

Development happens on Windows + Docker Desktop; all builds run in Linux
containers. Benchmark numbers for the paper come only from the bare-metal
Linux host in Table I.
