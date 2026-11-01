# rust-vs-jvm-runtime-tax

Reproducible artifact for *"Quantifying the Runtime Tax: Memory Inflation, CPU
Efficiency, and Cloud Infrastructure Costs of Native Rust vs. JVM Scala in Big
Data Platforms"* by Manoj Kumar Goud Sowdari and Akshay Kumar Goud Sowdari.

Two standalone pipelines run the **same workload** on the **same synthetic,
uncompressed Parquet dataset** inside **identical cgroup limits** (4 vCPU,
4 GB, no swap). One is native Rust and the other is Scala 3 on the JVM, with
no Spark or other framework. The harness measures:
- CPU time
- peak private memory
- per-batch latency
- GC pauses
- modeled cloud cost

Both pipelines produce bit-identical results, and every benchmark run checks
this.

No benchmark numbers are stored in this repository. Every figure and table is
regenerated from raw `results/` data.

## Layout

| Path | Contents |
|---|---|
| `datagen/` | Deterministic Parquet generator (Rust) |
| `pipeline-rust/` | Native pipeline: rustc 1.81.0, `arrow`/`parquet` 53.4.1; system or mimalloc allocator |
| `pipeline-scala/` | JVM pipeline: Scala 3.5.0, Temurin JDK 21.0.4+7, parquet-java 1.14.4 |
| `docker/` | Images (base images pinned by digest), in-container wrapper, core picker |
| `bench/` | `run.py` (production-style matrix), `heapsweep.py` (H_min search + heap sweep), `harness.py` (shared), `configs.yaml`, cgroup sampler, JFR parser, `verify.py` |
| `profiling/` | JFR settings (`gc.jfc`) |
| `analysis/` | `analyze.py` (statistics, figures, LaTeX tables), `heapsweep.py` (heap-sweep figures/tables), `compare_hosts.py` (VM vs metal), `cost.yaml` (rates you supply) |
| `scripts/` | `ec2-setup.sh` (idempotent EC2 host setup), `export-results.sh` (archive results before stopping an instance) |
| `results/` | Raw run output (git-ignored; schema in `results/README.md`) |
| `docs/` | `aws.md` (running on EC2), `paper-deltas.md` (facts the paper must reflect), `fat-jar-merge.md` (dependency merge audit) |
| `PLAN.md` | Design rationale and status log |

## The workload

The work is split into units of one row group in one file. Four workers pull
units from a shared counter and process each unit in batches of 8,192 rows.
For each batch:

1. **Materialize** every row into an owned record: a Rust `struct` with an
   owned `String`, or a Scala `case class`.
2. **Filter** rows to those with `metric > 75.0`.
3. **Transform** each kept row:
   - `hour_bucket = ts / 3,600,000`
   - `payload_hash = FNV-1a-64(payload)`
   - `tag` = the first 8 characters of `payload`, uppercased
4. **Aggregate** by `hour_bucket`: count, sum, min and max of `metric`, plus
   an XOR checksum that folds in `payload_hash`, `tag` and `id`. Folding them
   in means the compiler or JIT can't eliminate any of that work.
5. **Write** the 720 hourly rows to a Parquet file, then `fsync` it.

Partial results are merged in unit order, so floating-point sums are
reproducible and the two languages agree to the bit.

**Latency.** The primary metric is **per-unit latency**: the time to process
one full 65,536-row row group, including opening the file and preparing the
row group. Its scope is identical in both languages.

The secondary metric is **per-batch latency**: the time for one 8,192-row
batch, excluding setup. It is not symmetric, because parquet-java loads the
whole row group during setup while arrow-rs reads pages lazily inside
batches. See `PLAN.md` §3 for the full specification.

## Dataset presets

| Preset | Rows | Files | Size on disk | Free disk to provision |
|---|---|---|---|---|
| `dev` | 1,000,000 | 4 | ≈1.06 GB | 3 GB |
| `small` | 10,000,000 | 8 | ≈10.6 GB | 15 GB |
| `medium` | 95,000,000 | 16 | ≈100.7 GB | 130 GB (fits the c6id.4xlarge 950 GB instance store with `small` alongside) |
| `full` | 950,000,000 | 64 | ≈1.01 TB (0.92 TiB) | ≥1.2 TB; not used in the paper (does not fit the c6id.4xlarge instance store) |

The paper uses `small` for the heap sweep and `medium` for the H_min
validation and the production-style comparison. The c6id.metal validation
host runs `small` only.

Data is UNCOMPRESSED, PLAIN-encoded, with no dictionary, in row groups of
65,536 rows (≈69 MB). That row-group size keeps four in-flight row groups well
inside the 4 GB limit. Data is generated deterministically: seed 42 gives
byte-identical files for a given container image. `datagen` refuses to write
more than 100 GB without `--yes`.

## Requirements

**Benchmark hosts (for numbers that go in the paper).**

| Role | Instance | Host class |
|---|---|---|
| Primary | AWS `c6id.4xlarge` On-Demand: 8 physical cores × 2 SMT, Xeon Platinum 8375C, 32 GiB, 950 GB NVMe instance store, Ubuntu 24.04 | `cloud-vm` |
| Validation | AWS `c6id.metal` On-Demand (same CPU family, bare metal), small preset only | `bare-metal` |

**[docs/aws.md](docs/aws.md)** covers launching, setup
(`scripts/ec2-setup.sh`), running, exporting results before you stop the
instance, and the metal validation.

The harness refuses to record timings unless every gate of the selected
`--host-class` passes (`make aws-preflight` reports them all):
- **Both classes:** Linux; **cgroup v2** (`cgroup2fs`); page-cache drop (root,
  or the sudoers rule for `sysctl vm.drop_caches=3`); no Spot instance unless
  `--allow-spot`. Spot results are watermarked SMOKE TEST.
- **`bare-metal`:** the CPU does not report the `hypervisor` flag.
- **`cloud-vm`:**
  - EC2 detected via IMDSv2
  - instance type in the `configs.yaml` allowlist (c6id.4xlarge, c6id.metal)
  - data directory on the local NVMe instance store, not EBS

On `cloud-vm`:
- N = 20 runs per configuration (10 on bare metal)
- CPU steal on the pinned cores is sampled at 10 Hz
- runs whose steal exceeds 0.5% of their CPU time are flagged, and a
  sensitivity analysis excludes them
- the governor and turbo controls, normally not exposed in a VM, are recorded
  as "unavailable", along with the observed MHz per run

On bare metal, also set the CPU governor to `performance`, fix the boost
state, and disable SMT or isolate the benchmark cores. The manifest records
all of these.

**Development and correctness (any OS with Docker, or native):**
- rustc 1.81.0
- Temurin JDK 21.0.4+7
- sbt 1.10.2
- Python 3.12 with `bench/requirements.txt`

Windows and macOS machines, and Docker Desktop, are for correctness checks
only.

## Reproduce

```sh
# 1. Correctness (native toolchain): unit tests, then dev-preset end-to-end check
make build
make test PRESET=dev

# 2. Images
make images

# 3. Host check and data (on the benchmark host; on EC2 follow docs/aws.md first)
export HOST_CLASS=cloud-vm          # c6id.4xlarge; bare-metal on c6id.metal
make aws-preflight                  # report only
make datagen PRESET=small
make datagen PRESET=medium          # ≈100 GB; passes --yes for you

# 4. Calibrate: one rust-system + one scala-g1 run; prints projected run times
make calibrate

# 5. Heap-size sweep on the small preset, then validate its H_min on the medium preset
make heapsweep
make heapsweep-validate            # SWEEP=results/<ts>-heapsweep, default: latest

# 6. Production-style comparison on the medium preset (+ small, for compare-hosts)
make bench DATA=data/medium WARMUP_DATA=data/small
make bench DATA=data/small  WARMUP_DATA=data/small

# 7. Analysis (RESULTS=results/<ts>; default: latest bench or heap-sweep directory)
make plot
make plot RESULTS=results/<ts>-heapsweep VALIDATION=results/<ts>-heapsweep-validate
make compare-hosts VM=results/<c6id.4xlarge small ts> METAL=results/<c6id.metal ts>

# 8. Before stopping an EC2 instance: archive and copy off the results
make export-results EXPORT="results/<ts> ..."
```

| Experiment | Host / preset | What it gives |
|---|---|---|
| `make calibrate` | 4xlarge / small | measured per-run times and projected totals for everything below |
| `make heapsweep` | 4xlarge / small | H_min, measured off-heap reserves, CPU and M_peak vs heap size |
| `make heapsweep-validate` | 4xlarge / medium | whether the small-preset H_min holds on 95 M rows (3 attempts each at H_min and 1.25 × H_min) |
| `make bench` | 4xlarge / medium | production-style CPU, wall, M_peak, latency, GC pauses, cost |
| `make bench` | 4xlarge + metal / small | inputs to `make compare-hosts` (does the VM distort the Scala/Rust ratios?) |

**Two JVM experiments.**
- `make bench` runs the *production-style* JVM configuration: a fixed,
  pre-touched 3 GiB heap, as a capacity-planned service would run. Its memory
  figure reflects the setting.
- `make heapsweep` measures what each collector actually needs, following
  Hertz & Berger (OOPSLA 2005):
  1. Measure each collector's off-heap memory with one NMT run at a 3 GiB
     heap. Its reserve is 1.5× that (rounded up to 64 MiB, at least 512 MiB),
     and its heap cap is 4,096 MiB minus the reserve.
  2. Binary-search the smallest heap `H_min` that completes (128 MiB steps; a
     heap size passes only if 3 of 3 attempts complete), using exactly the
     sweep's JVM flags, JFR included.
  3. Run each collector at 1.25×, 1.5×, 2×, 3× and 4× its own `H_min`, with
     `-Xms = -Xmx` and **no pre-touch**. Points above the cap are skipped and
     recorded with the reason. Failed runs are not retried; the table shows
     completed/total, e.g. 9/10.
  4. Rust runs alongside as flat reference lines.

  Its outputs are CPU-seconds vs heap size and M_peak vs heap size.

`make bench` calls `bench/run.py`. Useful options:
- `--configs rust-system,scala-g1` runs a subset of configurations
- `--runs N` sets the number of measured runs (default 10)
- `--cpuset 2,4,6,8` pins specific cores. The default lets `docker/pick-cores.sh` choose 4 physical cores and leave their SMT siblings unused.
- `--seed S` fixes the run order (the default is random and is recorded)
- `--dry-run` tests the plumbing on a non-qualifying host. Its results are marked `-dryrun`, and the analysis watermarks them.
- `--host-class bare-metal|cloud-vm` (Make: `HOST_CLASS=`) selects the gates and the default N
- `--allow-spot` (Make: `ALLOW_SPOT=1`) permits a Spot instance for smoke tests; results are marked `-smoke` and watermarked

Fill in `analysis/cost.yaml` with a per-vCPU-hour rate and its source before
`make plot` if you want the cost table. No prices are hardcoded.

## What gets measured

| Metric | Source |
|---|---|
| CPU time | container cgroup `cpu.stat usage_usec` at exit, captured inside the container |
| Wall time | in-container start/end of the pipeline process (includes JVM startup); the pipeline's own main→fsync time is also logged |
| **M_peak** | max over 10 Hz host samples of `memory.stat anon + shmem`. `shmem` is required because ZGC backs its heap with memfd shared memory. |
| Page cache, `memory.current`, `memory.peak` | reported separately; never part of M_peak |
| Per-unit latency (primary) and per-batch latency (secondary) | `batches.csv` from both pipelines |
| H_min and heap-sweep curves | `bench/heapsweep.py` (JVM), with Rust reference runs in the same session |
| GC pauses, heap used/committed | JFR (`profiling/gc.jfc`): `jdk.GCPhasePause`, `jdk.GarbageCollection`, `jdk.GCHeapSummary` |
| JVM native memory | one extra NMT run per JVM config, excluded from timing statistics |
| Cost | provisioned (4 vCPU × wall time) and consumed vCPU-hours × your `cost.yaml` rate (the paper uses the c6id.4xlarge On-Demand price ÷ 16 vCPU) |
| CPU steal, observed MHz | `/proc/stat` steal and `/proc/cpuinfo` MHz on the pinned cores, 10 Hz |
| VM vs metal | `make compare-hosts`: ratios to `rust-system` on both hosts, with bootstrap CIs |

Run protocol:
- page cache dropped before every run
- 2 discarded JVM warm-up runs per JVM config
- N measured runs per config in randomized order: 20 on `cloud-vm`, 10 on `bare-metal`

Statistics:
- mean ± 95% CI (t-distribution), and the coefficient of variation
- Mann–Whitney U and Cliff's δ against `rust-system`
- cluster-bootstrap CIs (resampling whole runs) for p99 and p99.9 latency

Figures are 3.5-inch IEEE single-column PDF/PNG with embedded fonts, and
tables are booktabs `.tex` for `\input{}`.

## Configurations

Production-style matrix (`make bench`). The heap sweep reuses the same GC
flags with its own heap sizes and no pre-touch.

| Name | Runtime | Flags |
|---|---|---|
| `rust-system` | native | glibc malloc |
| `rust-mimalloc` | native | mimalloc 0.1.43 |
| `scala-g1` | JVM | `-Xms3G -Xmx3G -XX:+AlwaysPreTouch -XX:+UseG1GC` |
| `scala-zgc` | JVM | `-Xms3G -Xmx3G -XX:+AlwaysPreTouch -XX:+UseZGC -XX:+ZGenerational` |
| `scala-parallel` | JVM | `-Xms3G -Xmx3G -XX:+AlwaysPreTouch -XX:+UseParallelGC` |

All containers run with `--cpus=4 --memory=4g --memory-swap=4g
--cpuset-cpus=<4 physical cores> --network none`.

## Dependencies

Rust (exact versions pinned in `Cargo.lock`; MSRV 1.81):
- `arrow` 53.4.1 and `parquet` 53.4.1 (all 13 arrow/parquet crates on 53.4.1)
- `mimalloc` 0.1.43 (optional)
- datagen only: `rand` 0.8.5, `rand_chacha` 0.3.1, `rand_distr` 0.4.3

Scala (`pipeline-scala/build.sbt`):

| Dependency | Version | Why |
|---|---|---|
| `org.apache.parquet:parquet-hadoop` | 1.14.4 | Parquet reader/writer (`LocalInputFile`/`LocalOutputFile`) |
| `org.apache.hadoop:hadoop-client-api` | 3.3.6 | parquet-hadoop needs Hadoop `Configuration` and codec classes, which it declares as `provided` |
| `org.apache.hadoop:hadoop-client-runtime` | 3.3.6 | the shaded runtime for the above |
| `org.slf4j:slf4j-nop` | 2.0.16 | silences parquet-java logging |
| `org.scalameta:munit` | 1.0.2 | tests only |

Transitive runtime jars:
- from parquet-java: `parquet-column`, `parquet-common`, `parquet-encoding`, `parquet-format-structures`, `parquet-jackson`
- `jackson-core`/`databind`/`annotations`/`datatype-jdk8` 2.17.0
- `snappy-java`, `zstd-jni`, `aircompressor`
- `commons-pool`, `commons-logging`, `javax.annotation-api`, `jsr305`, `byte-buddy`
- the Scala 3 standard library

The fat-jar merge policy is explicit and was audited: there are no `.class`
conflicts. See `docs/fat-jar-merge.md`.

## Threats to validity

1. **Library differences are a confound.** The two pipelines share a
   workload, but each language uses a different Parquet library.
   - arrow-rs decodes columnar batches and reads pages lazily, so Rust's page
     I/O falls inside batch latency.
   - parquet-java's `readRowGroup` reads the whole row group up front (so
     Scala's I/O falls in `setup_nanos`) and assembles records through
     per-value `Binary` objects.

   Part of any measured gap is library design, not the language runtime.
2. **Heap sizing fixes the JVM's footprint.** In the production-style runs,
   `-Xms3G` with `AlwaysPreTouch` puts JVM M_peak at 3 GB or more by
   construction. Read that figure as "memory the configuration holds." The
   heap sweep measures "memory the workload needs."

   `H_min` depends on the dataset and on the 128 MiB search granularity, and
   it is measured on the small preset. Its 3-of-3 pass rule makes it
   conservative, but it is not a guarantee: a run near 1.25× `H_min` can
   still fail occasionally. Such failures are counted and reported per point.
3. **Hash maps follow each language's idiom:** Rust's std `HashMap` (SipHash)
   versus Scala's `mutable.LongMap` (open addressing, no key boxing).
4. **JIT warm-up is included.** Each run is a fresh process, as in a batch
   job. JVM warm-up runs warm the OS and disk, not the JIT.
5. **Recording overhead.** JFR is enabled on every JVM run, at low overhead
   with GC events only. Rust runs have no equivalent instrumentation.
6. **Scope.** One node, 4 vCPU, no distributed shuffle and no network. The
   results describe per-executor runtime cost, not cluster-level behavior.
7. **Isolation.** CPU pinning leaves SMT siblings unassigned but does not
   reserve them. Use `isolcpus` or disable SMT for strict isolation. The
   host state is recorded in `manifest.json`.
8. **The primary host is a VM** (EC2 `c6id.4xlarge`, Nitro). Known effects:
   - CPU steal and noisy neighbours. These are measured per run on the pinned
     cores; runs above 0.5% steal are flagged and a sensitivity analysis
     excludes them.
   - Frequency and turbo can't be controlled; only the observed MHz is
     recorded.
   - The idle SMT siblings are idle from the guest's view, not the
     hypervisor's.

   N = 20 runs per configuration and the coefficient of variation make the
   noise visible. The `c6id.metal` run on the small preset tests whether the
   Scala/Rust ratios differ between VM and metal (`make compare-hosts`).
   Results are not claimed to transfer to other instance families.

## Citation

See `CITATION.cff`. A Zenodo DOI will be added on release.

## License

Apache-2.0 (`LICENSE`).
