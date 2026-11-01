# Running the benchmark on AWS EC2

| Role | Instance | Hardware | Used for |
|---|---|---|---|
| **Primary** | `c6id.4xlarge`, On-Demand | 16 vCPU (8 physical cores × 2 SMT), Intel Xeon Platinum 8375C (Ice Lake), 32 GiB, 1 × 950 GB local NVMe instance store | everything: calibration, heap sweep (small), H_min validation and production-style comparison (medium) |
| **Validation** | `c6id.metal`, On-Demand | same CPU family, bare metal (128 vCPU, 256 GiB, 4 × 1,900 GB NVMe) | small preset only; checks that the VM does not distort the Scala/Rust ratios |

Both run Ubuntu 24.04 LTS. The harness refuses to record timings on any
other instance type or OS setup (see *Host gates* below).

> **Nothing here needs AWS credentials on the instance.** Do not copy access
> keys, `~/.aws`, or IAM user secrets onto it. If you use an instance profile
> for anything else, the benchmark does not need one.

## 1. Before launching

- **vCPU quota.** A `c6id.4xlarge` needs 16 vCPUs of the *Running On-Demand
  Standard (A, C, D, H, I, M, R, T, Z) instances* quota, and a `c6id.metal`
  needs 128. New accounts are often limited to fewer. Check Service Quotas →
  Amazon EC2 in your region, and request an increase a few days ahead.
- **Budget alert.** Create an AWS Budgets alert, e.g. a monthly cost budget
  with an email alert at 50% and 80%. On-Demand bills per second while the
  instance is *running*. A forgotten `c6id.metal` is expensive. Look up the
  current hourly prices for your region on the EC2 pricing page; nothing in
  this repository hardcodes them.
- **Key pair.** Create or import an SSH key pair in the region.
- **Security group.** Allow inbound **SSH (TCP 22) from your IP only**
  (`<your-ip>/32`). No other inbound rules are needed. Outbound: default
  (allow all), which is needed for apt, Docker Hub and GitHub.

## 2. Launch

- **AMI:** the official *Ubuntu Server 24.04 LTS (HVM), SSD Volume Type*
  published by Canonical (owner `099720109477`), x86_64. Record the AMI id;
  the harness also records it from instance metadata.
- **Instance type:** `c6id.4xlarge`.
- **Root volume:** **60 GB gp3**. Everything except the datasets lives on it:
  - Docker keeps images, build layers and build cache under
    `/var/lib/docker`. The two images are ~1 GB, but each `make images`
    rebuild adds multi-stage build cache: the Rust and sbt builder stages
    are several GB.
  - `results/` grows with every run (JFR recordings and their JSON, GC logs,
    10 Hz samples, batch logs), plus the export archives.
  - The OS, apt packages and `.venv` take a few GB more.

  30 GB is enough to start but can fill up mid-study, and a full root disk
  fails runs. `make aws-preflight` and the setup summary warn when `/` has
  less than 25 GB free. The datasets stay on the instance store.
- **Instance store:** attached automatically (`/dev/nvme1n1`, model
  "Amazon EC2 NVMe Instance Storage"). It is unformatted at first boot.
- **Purchasing option:**
  - **Spot** is fine for a *smoke test* (setup, `make calibrate` on a small
    run). The harness refuses timed runs on Spot unless `ALLOW_SPOT=1`, and
    then names the results `-smoke` and watermarks every figure and table
    **"SMOKE TEST — NOT FOR PUBLICATION"**. Spot can be reclaimed mid-run.
  - **On-Demand** for every run that goes into the paper.
- **IMDS:** keep the default **IMDSv2 required** (token-based). The harness
  only *reads* instance metadata: type, id, region, AZ, AMI and lifecycle.

## 3. Set up the instance

```sh
ssh -i <key>.pem ubuntu@<public-ip>

# The repository is private. Use ONE of:
#  (a) a read-only SSH deploy key, added under the repo's Settings → Deploy keys:
ssh-keygen -t ed25519 -f ~/.ssh/rvj_deploy -N ''     # paste ~/.ssh/rvj_deploy.pub as a deploy key
GIT_SSH_COMMAND='ssh -i ~/.ssh/rvj_deploy' \
  git clone git@github.com:manojkumargoudsowdari/rust-vs-jvm-runtime-tax.git
#  (b) a fine-grained personal access token limited to this repo with
#      Contents: Read-only, used once and not stored (git prompts for it):
git clone https://github.com/manojkumargoudsowdari/rust-vs-jvm-runtime-tax.git

cd rust-vs-jvm-runtime-tax
git checkout <tag>               # e.g. v0.3-aws; the manifest records the commit
scripts/ec2-setup.sh             # idempotent; DRY_RUN=1 to preview
exit                             # log out/in so the docker group applies
```

`scripts/ec2-setup.sh`:
- installs Docker Engine from Docker's apt repository, plus python3-venv, git,
  nvme-cli, jq and linux-tools if available
- formats the instance store as ext4 **only if it is blank**, mounts it at
  `/data` (noatime, `nofail` in fstab) and links `data/` → `/data/rvj`. It
  refuses the root/EBS disk and any disk that is partitioned or holds another
  filesystem.
- adds you to the `docker` group
- installs `/etc/sudoers.d/rvj-dropcaches`, which allows passwordless
  `/usr/sbin/sysctl vm.drop_caches=3` and nothing else
- creates `.venv` with the Python requirements
- prints a preflight summary

## 4. Run

### Spot smoke test (optional, before paying On-Demand)

On a **Spot** `c6id.4xlarge`, after section 3. This checks setup, the gates
and the run time, and nothing more. Every result lands in a `-smoke`
directory and is watermarked "SMOKE TEST — NOT FOR PUBLICATION".

```sh
cd rust-vs-jvm-runtime-tax
make aws-preflight HOST_CLASS=cloud-vm ALLOW_SPOT=1     # VERDICT cloud-vm PASS expected
make images
make datagen PRESET=small
make calibrate HOST_CLASS=cloud-vm ALLOW_SPOT=1         # -> results/<ts>-calibrate-smoke/
make export-results                                     # newest results dir -> results/exports/
# from your machine, then terminate the Spot instance:
scp -i <key>.pem 'ubuntu@<public-ip>:rust-vs-jvm-runtime-tax/results/exports/rvj-results-*' .
```

Without `ALLOW_SPOT=1`, the harness refuses timed runs on Spot, which is
intended. Nothing from a smoke test goes into the paper.

### Paper runs (On-Demand)

```sh
cd rust-vs-jvm-runtime-tax
make images
make aws-preflight                       # report only: gates, cores, /data, Docker
make datagen PRESET=small                # ~10.6 GB on /data
make calibrate HOST_CLASS=cloud-vm       # 1 Rust + 1 JVM run; projected hours; starts nothing long
```

Read the calibration projections before starting anything long. Then, in
`tmux` so an SSH drop doesn't kill the run:

```sh
make heapsweep HOST_CLASS=cloud-vm                           # small preset
make datagen PRESET=medium                                   # 95 M rows, ~100.7 GB
make heapsweep-validate HOST_CLASS=cloud-vm                  # H_min holds on medium?
make bench HOST_CLASS=cloud-vm DATA=data/medium WARMUP_DATA=data/small
make bench HOST_CLASS=cloud-vm DATA=data/small WARMUP_DATA=data/small   # for compare-hosts
make plot RESULTS=results/<ts>                               # per results directory
```

`HOST_CLASS=cloud-vm` uses N = 20 measured runs per configuration
(bare metal: 10).

## 5. Before you stop or terminate: export results

**The instance store is erased when the instance stops, hibernates or
terminates.** That wipes the datasets on `/data`. `results/` lives on the root
EBS volume, which survives a stop but **not** a terminate. Always:

```sh
make export-results EXPORT="results/<ts> results/<ts> ..."   # default: newest results dir
# then, from your own machine:
scp -i <key>.pem 'ubuntu@<public-ip>:rust-vs-jvm-runtime-tax/results/exports/rvj-results-*' .
sha256sum -c rvj-results-*.sha256
```

Only after the archive is on your machine: **terminate** the instance (EC2 →
Instances → Terminate) and check that no volumes or Elastic IPs are left
behind. After a stop/start (which you should avoid mid-study), re-create
`/data` with `scripts/ec2-setup.sh --storage-only` and regenerate the
datasets. Regeneration is deterministic, so they come back byte-identical.

## 6. Validation run on c6id.metal

Purpose: confirm that the VM does not distort the comparison. On a
`c6id.metal` (On-Demand, same AMI and setup steps):

```sh
make images && make aws-preflight        # bare-metal gate should PASS: no hypervisor flag
make datagen PRESET=small
make bench HOST_CLASS=bare-metal DATA=data/small WARMUP_DATA=data/small
make export-results
```

`c6id.metal` passes the bare-metal gate unchanged and is recorded as
`host_class=bare-metal`, with its EC2 identity in the manifest. It has 4
instance-store disks; the setup script uses the first. Then, anywhere with
both result sets:

```sh
make compare-hosts VM=results/<c6id.4xlarge small-preset ts> METAL=results/<c6id.metal ts>
```

This reports, per configuration, the ratio to `rust-system` for CPU-seconds,
M_peak and p99 per-unit latency on each host, with 95% bootstrap CIs, plus
the VM/metal ratio-of-ratios. If that CI covers 1, the VM did not measurably
distort the comparison. Terminate the metal instance as soon as it is
exported.

## Host gates (what the harness checks)

| Gate | `bare-metal` | `cloud-vm` |
|---|---|---|
| Linux, cgroup v2 (`cgroup2fs`) | required | required |
| page-cache drop (root or sudoers rule) | required | required |
| no `hypervisor` CPU flag | required | — |
| EC2 detected via IMDSv2 | — | required |
| instance type in `configs.yaml` allowlist (`c6id.4xlarge`, `c6id.metal`) | — | required |
| data directory on "Amazon EC2 NVMe Instance Storage" (not EBS) | — | required |
| Spot instance | refused unless `--allow-spot` (results watermarked SMOKE TEST) | same |

`make aws-preflight` prints every gate for both classes without running
anything. On a VM, cpufreq governor and turbo controls are usually not
exposed; the manifest records them as `unavailable`, along with the observed
MHz per run and CPU steal on the benchmark cores.
