# Paper deltas

Every fact in the paper draft (Sections I–II, as of 2026-10-23; updated
2026-10-31 for the AWS host change) that the
artifact contradicts, refines, or adds. Each entry gives the paper's current
text, what the artifact actually does, and suggested replacement wording.
Strike an entry once the LaTeX is updated.

Status key: **MUST** = paper is currently wrong or incomplete;
**ADD** = new information the paper needs; **WORDING** = framing.

---

## H. Hardware and environment (§II-A, Table I) — changed 2026-10-31

| # | Status | Paper says | Artifact uses | Suggested text |
|---|---|---|---|---|
| H1 | **MUST** | Table I: AMD EPYC 9354 bare-metal node, 256 GB, Docker 27.1.1 | **Primary host: AWS EC2 `c6id.4xlarge`** (On-Demand): 16 vCPU = 8 physical cores × 2 SMT, Intel Xeon Platinum 8375C (Ice Lake), 32 GiB, 1 × 950 GB local NVMe instance store; Ubuntu 24.04 LTS (Canonical AMI); Docker Engine from Docker's apt repository. **Validation host: `c6id.metal`** (same CPU family, bare metal), small preset only. Exact AMI id, kernel, Docker version and instance id are in each `manifest.json`. | replace Table I with both instances; take versions from the manifests, not from this file |
| H2 | **MUST** | "dedicated, single-node bare-metal architecture to eliminate the noisy-neighbor multi-tenancy effects" | primary runs are on a **virtual machine** (Nitro). Mitigations: 4 physical cores pinned with their SMT siblings left idle; data on local NVMe instance store (not network EBS); CPU steal sampled at 10 Hz per run on the pinned cores; runs with steal > 0.5 % of their CPU time are flagged and a sensitivity analysis excludes them; N = 20 runs per configuration; coefficient of variation reported; validation of the Scala/Rust ratios on `c6id.metal` | rewrite §II-A around the VM + validation design; drop the "eliminate noisy-neighbor" claim |
| H3 | ADD | — | host classes: `cloud-vm` timed runs only on an allowlisted EC2 type (IMDSv2-verified), On-Demand, data on instance store; Spot runs are smoke tests only (watermarked) | one sentence in Protocol |
| H4 | ADD | — | cpufreq governor / turbo controls are not exposed in the VM (recorded as "unavailable"); observed MHz per run is recorded from `/proc/cpuinfo` | mention in Threats |
| H5 | ADD | — | VM-vs-metal comparison: per configuration, ratio to `rust-system` for CPU-seconds, M_peak and p99 per-unit latency on both hosts, with 95% bootstrap CIs and the VM/metal ratio-of-ratios | new paragraph + `table_compare_hosts` / `fig_compare_hosts` |

## A. Software versions (§II-B)

| # | Status | Paper says | Artifact uses | Suggested text |
|---|---|---|---|---|
| A1 | ADD | — (no Rust library named) | `arrow` / `parquet` crates **53.4.1** (all 13 arrow/parquet crates locked to 53.4.1) | "…reads Parquet via the Apache Arrow Rust implementation (`arrow`/`parquet` crates 53.4.1)…" |
| A2 | ADD | — (no JVM library named) | parquet-java (`parquet-hadoop`) **1.14.4**, `hadoop-client-api`/`hadoop-client-runtime` **3.3.6** (shaded), `slf4j-nop` 2.0.16 | "…reads Parquet via parquet-java 1.14.4 with `LocalInputFile`; Hadoop 3.3.6 shaded client jars supply configuration classes only…" |
| A3 | OK | rustc 1.81.0, `--release`, `opt-level = 3` | same, **plus** `lto = "fat"`, `codegen-units = 1`, `panic = "abort"` | add the three extra profile settings |
| A4 | OK | Scala 3.5.0, JDK 21.0.4 | Scala 3.5.0, Temurin 21.0.4+7, sbt 1.10.2, sbt-assembly 2.2.0 | name the Temurin build and the build tools |
| A5 | ADD | — | base images pinned by digest (see `docker/*.Dockerfile`). The image builds fail unless the JDK is exactly Temurin-21.0.4+7 and rustc/cargo are 1.81.0. Each results directory records the versions read from inside the images, and every JVM run's JDK version from JFR (the analysis refuses mixed versions). | one sentence + artifact DOI |

## B. Configurations (§II-B)

| # | Status | Paper says | Artifact uses | Suggested text |
|---|---|---|---|---|
| B1 | MUST | `-XX:+UseZGC` | `-XX:+UseZGC -XX:+ZGenerational` (generational ZGC; on JDK 21 the flag is required, otherwise single-generation ZGC runs) | "…the generational Z Garbage Collector (`-XX:+UseZGC -XX:+ZGenerational`)…" |
| B2 | ADD | two GC variants (ZGC, G1) | **three**: G1, generational ZGC, **ParallelGC** (`-XX:+UseParallelGC`, throughput-oriented reference) | add ParallelGC as a third sub-variant |
| B3 | ADD | one Rust variant | **two**: system allocator (glibc malloc) and **mimalloc** 0.1.43 | "…benchmarked with the system allocator and with mimalloc, to separate allocator cost from GC cost." |
| B4 | ADD | `-Xms3G -Xmx3G` | plus `-XX:+AlwaysPreTouch` on all JVM runs; JFR (`profiling/gc.jfc`) and `-Xlog:gc*` enabled | list all JVM flags in a table |
| B5 | ADD | — | one extra **NMT run** per JVM config (`-XX:NativeMemoryTracking=summary`), excluded from timing | describe as memory-accounting run |
| B6 | **MUST** | — | **Heap-size sweep (core GC-tax result; Hertz & Berger, OOPSLA 2005 methodology).** (0) Per collector, measure the off-heap reserve: one NMT run at a 3,072 MiB heap on the sweep preset; reserve_c = max(512, ⌈1.5 × non-heap committed / 64⌉ × 64) MiB; heap cap_c = 4,096 − reserve_c (measured values reported in the H_min table). (1) Binary-search H_min = smallest `-Xms=-Xmx` (128 MiB granularity) at which the small preset completes; a probe passes only if 3/3 attempts succeed; probes use exactly the sweep's JVM flags, JFR included. (2) Run each collector at 1.25×, 1.5×, 2×, 3×, 4× its own H_min, skipping points above cap_c (reported with the reason). **No `-XX:+AlwaysPreTouch`**, so resident memory reflects use. (3) N = 10 measured runs per point plus 1 warm-up; failures are counted, not retried, and reported as completed/total (e.g. 9/10 at 1.25×); record CPU-seconds, wall, M_peak, GC pause percentiles, total pause time and total GC-cycle time (JFR). (4) Rust (both allocators) as flat reference lines measured in the same session. Figures: CPU-seconds vs heap multiple; M_peak vs heap multiple. | new subsection in Methodology + Results; cite Hertz & Berger 2005 |
| B7 | **MUST** | `-Xms3G -Xmx3G` described as *the* JVM configuration | the fixed `-Xms3G -Xmx3G -XX:+AlwaysPreTouch` configuration is retained and labelled **"production-style"** (capacity-planned service); its M_peak reflects the setting, not the need. The heap sweep (B6) measures the need. | rename it "production-style" wherever it appears |

## C. Dataset (§II-C)

| # | Status | Paper says | Artifact uses | Suggested text |
|---|---|---|---|---|
| C1 | **MUST** | "1-Terabyte … containing 1.2 billion rows" | **production-style comparison uses the medium preset: 95,000,000 rows ≈ 100.7 GB** (≈1,060 bytes/row on disk), 16 files. The 1 TB scale is not used: it would not fit the c6id.4xlarge 950 GB instance store with headroom, and the per-run cost would preclude N = 20. (1.2 B rows would be ≈ 1.27 TB.) | "…a synthetic, uncompressed ≈100 GB dataset of 95 million rows…"; remove "1-Terabyte" from the abstract and §II-C |
| C2 | ADD | schema `{id: UUID, ts: Int64, payload: String(1024), metric: Float64}` | id = FixedSizeBinary(16) UUIDv4; ts = epoch-ms uniform over 30 days from 2024-01-01; payload = exactly 1024 chars from `[A-Za-z0-9_-]`; metric ~ Normal(100, 25) clamped ≥ 0; all columns REQUIRED | give the distributions |
| C3 | ADD | — | **row groups of 65,536 rows (~69 MB)**; medium preset = 16 files (90 full row groups each) | "Row groups hold 65,536 rows (≈69 MB) so that four concurrent row groups fit well within the 4 GB envelope." |
| C4 | ADD | "uncompressed" | UNCOMPRESSED, dictionary **disabled**, PLAIN encoding, data page v1 | state all three |
| C5 | ADD | — | deterministic generator (ChaCha8, seed 42, one stream per file); byte-identical regeneration | reproducibility sentence |
| C6 | **MUST** | one dataset for everything | **Preset plan (c6id.4xlarge):** heap sweep on the **small** preset (10 M rows, ≈10.6 GB); H_min validated on the **medium** preset (per collector, 3 attempts at the small-preset H_min and at ⌈1.25 × H_min⌉); production-style comparison on the **medium** preset (95 M rows, ≈100.7 GB). **c6id.metal:** production-style comparison on the small preset only, compared against the c6id.4xlarge small-preset run (H5). | "Because the workload streams with bounded state (720 hourly groups; four row groups in flight), the minimum heap should not depend on dataset size. We determine H_min on a 10 M-row dataset and verify it on the 95 M-row dataset [result: agree / disagree]." Report a disagreement if it occurs. |

## D. Workload (paper has no workload section yet — §IV)

| # | Status | Content |
|---|---|---|
| D1 | **MUST** | Exact steps, identical in both languages, per (file, row group) unit, in batches of **8,192 rows**: (1) materialize each row into an owned language-native record (Rust `struct` with owned `String`; Scala `case class`); (2) filter `metric > 75.0`; (3) transform: `hour_bucket = ts / 3,600,000`, `payload_hash = FNV-1a-64(payload)`, `tag = upper(payload[0..8])`; (4) aggregate by `hour_bucket`: count, sum, min, max of `metric`, and XOR of `payload_hash ⊕ FNV-1a-64(tag) ⊕ id_lo ⊕ id_hi`; (5) write the 720 hourly aggregates, sorted, to one Parquet file and `fsync`. |
| D2 | ADD | Parallelism: 4 workers pull (file, row group) units from a shared counter (Rust: `std::thread::scope`; Scala: `Executors.newFixedThreadPool(4)`). |
| D3 | ADD | Correctness: both pipelines produce **bit-identical** aggregates (partials merged in unit order), verified on the dev (1 M rows) and small (10 M rows) presets for all five configurations. |
| D4 | **MUST** | Latency metrics. **Primary: per-unit latency** = time to process one full 65,536-row row-group unit = unit setup (open file, read footer, prepare/load the row group) + all of its 8,192-row batches. The scope is identical in both languages. (Each file's last, shorter row group is excluded.) **Secondary: per-batch latency** = time to process one 8,192-row batch (decode + materialize + filter + transform + aggregate), excluding setup. It is asymmetric: parquet-java's `readRowGroup` reads the row group during setup, while arrow-rs reads pages lazily inside batches, so Rust's I/O is inside batch latency and Scala's is not. Report p50/p95/p99/p99.9 with cluster-bootstrap CIs (resampling whole runs). |

## E. Measurement (§II-D)

| # | Status | Paper says | Artifact does | Suggested text |
|---|---|---|---|---|
| E1 | **MUST** | M_peak = max RSS(t) | cgroup v2 `memory.peak` includes page cache, which a streaming read fills to the limit for every configuration. **M_peak = max over 10 Hz samples of `memory.stat anon + shmem`.** `shmem` must be included: ZGC backs the Java heap with memfd shared memory, which the kernel accounts as `shmem` (inside `file`), not `anon` — `anon` alone under-reports ZGC by the full heap size (≈3 GB). Page cache (`file − shmem`), `memory.current` and `memory.peak` are reported separately. | "M_peak is the peak private memory of the container cgroup, `anon + shmem`, sampled at 10 Hz; page cache is excluded and reported separately. Shared memory is included because ZGC maps its heap through memfd." |
| E2 | **MUST** | "using a detached native eBPF (Extended Berkeley Packet Filter) host profiler" | host-side sampler reading cgroup v2 `memory.stat`/`cpu.stat` (plus `memory.current`, `memory.peak`) at 10 Hz; final totals captured in-container at exit. **No eBPF is used.** | replace "a detached native eBPF (Extended Berkeley Packet Filter) host profiler" with "cgroup v2 `memory.stat`/`cpu.stat` counters sampled at 10 Hz" |
| E3 | ADD | E_compute integral of CPU utilization | `cpu.stat usage_usec` of the container cgroup at exit (exact kernel accounting, equals the integral) | note equivalence |
| E3b | ADD | "execution lifetime … from the initial file descriptor read to the final durable disk synchronization" | wall time = pipeline process start → exit inside the container (includes JVM startup), measured with `/proc/uptime` (not stepped by NTP; 10 ms resolution); the pipeline's own main → output `fsync` time is also recorded | state both and which one tables use (`wall_ns`) |
| E4 | ADD | GC pauses via JFR | `jdk.GCPhasePause` (pause distribution, total pause time) + `jdk.GarbageCollection` (GC count; total GC-cycle time = sum of cycle durations, wall time incl. concurrent phases) + `jdk.GCHeapSummary` (heap used/committed), parsed with `jfr print --json`; ZGC's cost appears mainly as concurrent CPU, not pauses | mention the event types, both totals, and the ZGC caveat |
| E5 | **MUST** | "e.g. AWS EC2 On-Demand pricing for general-purpose instances" | both **provisioned** (4 vCPU × wall time — what clouds bill) and **consumed** (`usage_usec`) vCPU-hours, priced with the **c6id.4xlarge On-Demand rate** for the run's region, per vCPU-hour = instance hourly price ÷ 16; the user fills `analysis/cost.yaml` with the price, source URL and retrieval date (no hardcoded prices) | "Costs use the c6id.4xlarge On-Demand price in <region> as of <date>, apportioned per vCPU" |
| E6 | **MUST** | — | protocol: page cache dropped before every run; 2 discarded JVM warm-up runs on the small preset; **N = 20 measured runs per configuration on c6id.4xlarge** (`cloud-vm`), N = 10 on c6id.metal (`bare-metal`), all in randomized interleaved order | add a "Protocol" paragraph |
| E7 | ADD | — | statistics: mean ± 95 % CI (t), coefficient of variation, Mann–Whitney U with Cliff's δ, cluster-bootstrap CIs (resampling runs) for p99/p99.9; a quantile q is reported only with ≥ 10/(1−q) samples (p99 ≥ 1,000; p99.9 ≥ 10,000). Medium preset: 1,440 full units per run × 20 runs = 28,800 → p99.9 supported. Small preset on metal: 152 × 10 = 1,520 → p99 only. | add a "Statistical treatment" paragraph |
| E8 | ADD | — | CPU steal: sampled at 10 Hz from `/proc/stat` on the pinned cores; per run total steal and steal ÷ CPU time; runs above 0.5 % flagged; flagged counts per configuration and a sensitivity analysis excluding flagged runs (`table_steal_sensitivity`) | one sentence in Protocol + Threats |

## F. Framing (Abstract, §I, §II)

| # | Status | Issue |
|---|---|---|
| F1 | WORDING | Abstract states results ("eliminates long-tail p99 latency spikes", "reduces overall cloud infrastructure costs") before measurements exist; restate as hypotheses until data supports them. |
| F2 | MUST | §I roadmap says §II = related work, §III = setup; the draft's §II is the setup and there is no related-work section. |
| F3 | WORDING | "airtight", "peer-reviewed evaluation", "exact GC tax", "completely eliminating" — soften. |
| F4 | WORDING | Title/scope: single node, 4 vCPU, no distributed shuffle; scope the claim to per-executor runtime cost. |
| F5 | ADD | Threats to validity: arrow-rs vs parquet-java library differences (eager row-group load vs lazy page reads, per-value `Binary` objects); hash-map idiom (SipHash `HashMap` vs `LongMap`); heap sizing fixes JVM footprint; JIT warm-up included in wall time. **VM effects (primary host is a Nitro VM):** CPU steal and noisy neighbours (measured and flagged, sensitivity analysis), frequency/turbo not controllable or observable beyond `/proc/cpuinfo` MHz, SMT siblings idle but not reserved from the hypervisor's view, and possible differences from bare metal (quantified by the c6id.metal validation on the small preset only). |
| F6 | WORDING | Abstract and §I say "1-Terabyte" and "bare-metal"; update to the 95 M-row / ≈100 GB dataset on c6id.4xlarge with c6id.metal validation. |
