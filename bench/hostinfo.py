"""Host validation and environment capture for manifest.json.

Gate logic is split into `collect_facts()` (touches the system) and
`evaluate_gates()` (pure), so every host-class decision is unit-tested in
bench/tests/test_hostinfo.py.
"""

from __future__ import annotations

import glob
import os
import platform
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

HOST_CLASSES = ("bare-metal", "cloud-vm")
UNAVAILABLE = "unavailable"
IMDS = "http://169.254.169.254/latest"
SYSCTL = "/usr/sbin/sysctl"


def _read(path: str | Path) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _run(*cmd: str) -> str | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def cgroup_fs_type() -> str | None:
    return _run("stat", "-fc", "%T", "/sys/fs/cgroup")


def hypervisor_flag() -> bool:
    cpuinfo = _read("/proc/cpuinfo") or ""
    return bool(re.search(r"^flags\s*:.*\bhypervisor\b", cpuinfo, re.M))


# --------------------------------------------------------------------------- EC2 (IMDSv2)

def ec2_metadata(timeout: float = 1.0) -> dict | None:
    """Instance identity via IMDSv2 (session token); None when not on EC2."""
    try:
        req = urllib.request.Request(f"{IMDS}/api/token", method="PUT",
                                     headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"})
        token = urllib.request.urlopen(req, timeout=timeout).read().decode()
    except (urllib.error.URLError, OSError, ValueError):
        return None

    def get(path: str) -> str | None:
        try:
            r = urllib.request.Request(f"{IMDS}/meta-data/{path}", headers={"X-aws-ec2-metadata-token": token})
            return urllib.request.urlopen(r, timeout=timeout).read().decode().strip()
        except (urllib.error.URLError, OSError, ValueError):
            return None

    return {
        "instance_type": get("instance-type"),
        "instance_id": get("instance-id"),
        "region": get("placement/region"),
        "availability_zone": get("placement/availability-zone"),
        "ami_id": get("ami-id"),
        "lifecycle": get("instance-life-cycle"),  # "on-demand" | "spot" | "scheduled"
    }


# --------------------------------------------------------------------------- storage

def _disk_models(dev_name: str, seen: set[str] | None = None) -> list[str]:
    """Models of the physical disk(s) behind a block device (follows partitions, dm, md)."""
    seen = seen if seen is not None else set()
    if dev_name in seen:
        return []
    seen.add(dev_name)
    block = Path("/sys/class/block") / dev_name
    if not block.exists():
        return []
    slaves = sorted(p.name for p in (block / "slaves").glob("*")) if (block / "slaves").is_dir() else []
    if slaves:  # dm-crypt, LVM, md RAID
        return [m for s in slaves for m in _disk_models(s, seen)]
    if (block / "partition").exists():  # nvme1n1p1 -> nvme1n1
        return _disk_models(block.resolve().parent.name, seen)
    model = _read(block / "device" / "model")
    return [model] if model else []


def storage_for(path: Path) -> dict:
    """Mount source, filesystem and disk model(s) holding `path`."""
    target = path.resolve()
    out = _run("findmnt", "-n", "-o", "SOURCE,FSTYPE,TARGET", "--target", str(target)) or ""
    parts = out.split()
    # Bind mounts report "SOURCE[/subpath]", e.g. "/dev/sde[/data/docker/volumes]".
    source = parts[0].split("[", 1)[0] if parts else None
    dev = Path(source).resolve().name if source and source.startswith("/dev/") else None
    return {
        "path": str(target),
        "source": source,
        "fstype": parts[1] if len(parts) > 1 else None,
        "mountpoint": parts[2] if len(parts) > 2 else None,
        "device": dev,
        "disk_models": _disk_models(dev) if dev else [],
    }


# --------------------------------------------------------------------------- gates

def collect_facts(data_path: Path | None) -> dict:
    is_linux = platform.system() == "Linux"
    return {
        "os": platform.system(),
        "cgroup_fs": cgroup_fs_type() if is_linux else None,
        "hypervisor_flag": hypervisor_flag() if is_linux else None,
        "can_drop_caches": can_drop_caches(),
        "ec2": ec2_metadata() if is_linux else None,
        "data_storage": storage_for(data_path) if is_linux and data_path else None,
    }


def evaluate_gates(host_class: str, facts: dict, policy: dict, *, allow_spot: bool = False,
                   want_drop: bool = True) -> list[str]:
    """Reasons timings must NOT be recorded on this host (empty list = all gates pass).

    bare-metal: Linux, cgroup v2, no `hypervisor` CPU flag, page-cache drop.
    cloud-vm:   Linux, cgroup v2, page-cache drop, EC2 detected via IMDSv2,
                instance type in the allowlist, data on the local NVMe instance store.
    Both:       on EC2, a Spot instance is refused unless allow_spot (smoke tests).
    """
    if host_class not in HOST_CLASSES:
        return [f"unknown host class {host_class!r} (expected one of {', '.join(HOST_CLASSES)})"]
    if facts.get("os") != "Linux":
        return [f"host OS is {facts.get('os')}, not Linux"]
    reasons = []
    if facts.get("cgroup_fs") != "cgroup2fs":
        reasons.append(f"/sys/fs/cgroup is {facts.get('cgroup_fs')!r}, not cgroup2fs (cgroup v2 required)")
    if want_drop and not facts.get("can_drop_caches"):
        reasons.append(f"cannot drop the page cache: run as root or allow `sudo {SYSCTL} vm.drop_caches=3` "
                       "(scripts/ec2-setup.sh installs that sudoers rule)")
    ec2 = facts.get("ec2")

    if host_class == "bare-metal":
        if facts.get("hypervisor_flag"):
            reasons.append("CPU reports the 'hypervisor' flag: this is a VM, not bare metal "
                           "(use --host-class cloud-vm on an allowlisted EC2 instance)")
    else:
        vm = policy.get("cloud_vm", {})
        if not ec2 or not ec2.get("instance_type"):
            reasons.append("EC2 not detected via IMDSv2 (cloud-vm timed runs are only allowed on EC2)")
        else:
            allow = vm.get("instance_allowlist", [])
            if ec2["instance_type"] not in allow:
                reasons.append(f"instance type {ec2['instance_type']} is not in the allowlist {allow}")
        storage = facts.get("data_storage") or {}
        model = vm.get("instance_store_model", "Amazon EC2 NVMe Instance Storage")
        models = storage.get("disk_models") or []
        if not models or any(m != model for m in models):
            reasons.append(f"data directory {storage.get('path')} is on {models or 'an unknown device'} "
                           f"(device {storage.get('device')}), not the local NVMe instance store ({model!r})")

    if ec2 and ec2.get("lifecycle") == "spot" and not allow_spot:
        reasons.append("this is a Spot instance: pass --allow-spot for smoke tests (results are watermarked "
                       "SMOKE TEST and must not be published)")
    return reasons


def is_smoke_test(facts: dict) -> bool:
    return bool((facts.get("ec2") or {}).get("lifecycle") == "spot")


# --------------------------------------------------------------------------- CPU topology and counters

def thread_siblings(cpus: list[int]) -> dict[str, str | None]:
    return {str(c): _read(f"/sys/devices/system/cpu/cpu{c}/topology/thread_siblings_list") for c in cpus}


def parse_cpulist(spec: str | None) -> list[int]:
    """'1,3,5-7' -> [1, 3, 5, 6, 7]."""
    out: list[int] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        lo, _, hi = part.partition("-")
        out.extend(range(int(lo), int(hi or lo) + 1))
    return out


def steal_ticks(cpus: list[int] | None, text: str | None = None) -> int | None:
    """Cumulative steal ticks (USER_HZ) summed over `cpus` (None = all CPUs) from /proc/stat."""
    text = _read("/proc/stat") if text is None else text
    if text is None:
        return None
    wanted = None if not cpus else {f"cpu{c}" for c in cpus}
    total = 0
    for line in text.splitlines():
        f = line.split()
        if not f or not f[0].startswith("cpu"):
            continue
        if (wanted is None and f[0] == "cpu") or (wanted is not None and f[0] in wanted):
            total += int(f[8]) if len(f) > 8 else 0  # user nice system idle iowait irq softirq STEAL
    return total


def ticks_to_usec(ticks: int) -> float:
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    return ticks * 1_000_000 / hz


def cpu_mhz(cpus: list[int] | None, text: str | None = None) -> float | None:
    """Mean 'cpu MHz' over `cpus` (None = all) from /proc/cpuinfo."""
    text = _read("/proc/cpuinfo") if text is None else text
    if not text:
        return None
    wanted = set(cpus) if cpus else None
    vals, proc = [], None
    for line in text.splitlines():
        if line.startswith("processor"):
            proc = int(line.split(":")[1])
        elif line.startswith("cpu MHz") and (wanted is None or proc in wanted):
            vals.append(float(line.split(":")[1]))
    return sum(vals) / len(vals) if vals else None


# --------------------------------------------------------------------------- manifest sections

def _lscpu() -> dict[str, str]:
    out = _run("lscpu") or ""
    return {k.strip(): v.strip() for k, _, v in (line.partition(":") for line in out.splitlines()) if v}


def _distinct(paths: str) -> list[str] | str:
    vals = sorted({v for p in glob.glob(paths) if (v := _read(p)) is not None})
    return vals or UNAVAILABLE


def boost_state() -> dict[str, str]:
    return {
        "cpufreq_boost": _read("/sys/devices/system/cpu/cpufreq/boost") or UNAVAILABLE,
        "intel_pstate_no_turbo": _read("/sys/devices/system/cpu/intel_pstate/no_turbo") or UNAVAILABLE,
        "amd_pstate_status": _read("/sys/devices/system/cpu/amd_pstate/status") or UNAVAILABLE,
    }


def os_release() -> str | None:
    text = _read("/etc/os-release") or ""
    m = re.search(r'^PRETTY_NAME="?([^"\n]*)"?', text, re.M)
    return m.group(1) if m else None


def host_section() -> dict:
    lscpu = _lscpu()
    meminfo = _read("/proc/meminfo") or ""
    mem_kb = re.search(r"MemTotal:\s+(\d+)", meminfo)
    return {
        "os": platform.system(),
        "os_release": os_release(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "cpu_model": lscpu.get("Model name"),
        "cpus_logical": os.cpu_count(),
        "threads_per_core": lscpu.get("Thread(s) per core"),
        "sockets": lscpu.get("Socket(s)"),
        "numa_nodes": lscpu.get("NUMA node(s)"),
        "mem_total_bytes": int(mem_kb.group(1)) * 1024 if mem_kb else None,
        # cpufreq/turbo controls are usually not exposed inside an EC2 VM.
        "cpu_governors": _distinct("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"),
        "cpu_drivers": _distinct("/sys/devices/system/cpu/cpu*/cpufreq/scaling_driver"),
        "boost": boost_state(),
        "smt_control": _read("/sys/devices/system/cpu/smt/control") or UNAVAILABLE,
        "smt_active": _read("/sys/devices/system/cpu/smt/active") or UNAVAILABLE,
        "cgroup_fs": cgroup_fs_type(),
        "hypervisor_flag": hypervisor_flag(),
        "systemd_detect_virt": _run("systemd-detect-virt") if shutil.which("systemd-detect-virt") else None,
        "kernel_cmdline": _read("/proc/cmdline"),
        "transparent_hugepage": _read("/sys/kernel/mm/transparent_hugepage/enabled"),
        "python": platform.python_version(),
    }


def clock_state() -> dict:
    """NTP/chrony state plus a realtime-vs-monotonic pair. Comparing the start and
    end snapshots reveals a clock step during the run: realtime_minus_monotonic_ns
    changes only by slewing (small) unless the realtime clock was stepped."""
    import time
    show = _run("timedatectl", "show") if shutil.which("timedatectl") else None
    realtime, monotonic = time.time_ns(), time.monotonic_ns()
    return {
        "realtime_ns": realtime,
        "monotonic_ns": monotonic,
        "realtime_minus_monotonic_ns": realtime - monotonic,
        "timedatectl_show": dict(l.split("=", 1) for l in show.splitlines() if "=" in l) if show else None,
        "chronyc_tracking": _run("chronyc", "tracking") if shutil.which("chronyc") else None,
    }


def docker_section(image_refs: dict[str, str]) -> dict:
    info = _run("docker", "info", "--format",
                "{{.ServerVersion}}|{{.CgroupDriver}}|{{.CgroupVersion}}|{{.KernelVersion}}") or "|||"
    server, driver, cgv, kernel = info.split("|")
    images = {}
    for name, ref in image_refs.items():
        fmt = "{{.Id}}|{{json .RepoDigests}}|{{json .Config.Labels}}"
        raw = _run("docker", "image", "inspect", "--format", fmt, ref)
        if raw:
            image_id, digests, labels = raw.split("|", 2)
            images[name] = {"ref": ref, "id": image_id, "repo_digests": digests, "labels": labels}
        else:
            images[name] = {"ref": ref, "id": None}
    return {
        "server_version": server or None,
        "client_version": _run("docker", "version", "--format", "{{.Client.Version}}"),
        "cgroup_driver": driver or None,
        "cgroup_version": cgv or None,
        "kernel_version": kernel or None,
        "images": images,
    }


def git_section(repo: Path) -> dict:
    sha = _run("git", "-C", str(repo), "rev-parse", "HEAD")
    status = _run("git", "-C", str(repo), "status", "--porcelain")
    return {
        "sha": sha,
        "dirty": bool(status) if status is not None else None,
        "describe": _run("git", "-C", str(repo), "describe", "--tags", "--always", "--dirty"),
    }


# --------------------------------------------------------------------------- page cache

def can_drop_caches() -> bool:
    if platform.system() != "Linux":
        return False
    if os.geteuid() == 0:
        return os.access("/proc/sys/vm/drop_caches", os.W_OK)
    # `sudo -l CMD` checks the sudoers rule without running it (works with the
    # single-command rule from scripts/ec2-setup.sh as well as full sudo).
    return subprocess.run(["sudo", "-n", "-l", SYSCTL, "vm.drop_caches=3"], capture_output=True).returncode == 0


def drop_caches() -> None:
    os.sync()
    if os.geteuid() == 0:
        Path("/proc/sys/vm/drop_caches").write_text("3\n")
    else:
        subprocess.run(["sudo", "-n", SYSCTL, "vm.drop_caches=3"], check=True, capture_output=True)
