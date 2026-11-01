"""Host-side 10 Hz sampler of a container's cgroup v2 memory and CPU counters."""

from __future__ import annotations

import csv
import threading
import time
from pathlib import Path

import hostinfo

CGROUP_ROOT = Path("/sys/fs/cgroup")
# memory.stat keys recorded per sample; `anon` is the primary memory metric.
STAT_KEYS = ["anon", "file", "kernel", "kernel_stack", "pagetables", "sock", "shmem", "file_mapped"]
COLUMNS = ["t_ns", "memory_current", "memory_peak", *STAT_KEYS, "cpu_usage_usec", "steal_ticks", "cpu_mhz"]


def container_cgroup(pid: int, container_id: str) -> Path | None:
    """Resolve the cgroup v2 directory of a running container."""
    try:
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                path = CGROUP_ROOT / line[3:].lstrip("/")
                if path.is_dir():
                    return path
    except OSError:
        pass
    for candidate in (CGROUP_ROOT / "system.slice" / f"docker-{container_id}.scope",
                      CGROUP_ROOT / "docker" / container_id):
        if candidate.is_dir():
            return candidate
    return None


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _read_kv(path: Path) -> dict[str, int]:
    out = {}
    for line in path.read_text().splitlines():
        k, _, v = line.partition(" ")
        if v.isdigit():
            out[k] = int(v)
    return out


class CgroupSampler(threading.Thread):
    """Samples until stopped or until the cgroup directory disappears."""

    def __init__(self, cgroup: Path, hz: float, t0_ns: int, cpus: list[int] | None = None):
        super().__init__(daemon=True)
        self.cgroup, self.period, self.t0_ns = cgroup, 1.0 / hz, t0_ns
        self.cpus = cpus  # the run's cores, for steal and MHz (None = all CPUs)
        self.rows: list[dict] = []
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()
        self.join()

    def run(self) -> None:
        next_t = time.monotonic()
        while not self._halt.is_set():
            try:
                stat = _read_kv(self.cgroup / "memory.stat")
                cpu = _read_kv(self.cgroup / "cpu.stat")
            except OSError:
                break  # container exited and its cgroup was removed
            row = {
                "t_ns": time.monotonic_ns() - self.t0_ns,
                "memory_current": _read_int(self.cgroup / "memory.current"),
                "memory_peak": _read_int(self.cgroup / "memory.peak"),
                "cpu_usage_usec": cpu.get("usage_usec"),
                "steal_ticks": hostinfo.steal_ticks(self.cpus),  # cumulative, USER_HZ
                "cpu_mhz": hostinfo.cpu_mhz(self.cpus),         # observed, from /proc/cpuinfo
            }
            row.update({k: stat.get(k) for k in STAT_KEYS})
            self.rows.append(row)
            next_t += self.period
            self._halt.wait(max(0.0, next_t - time.monotonic()))

    def write_csv(self, path: Path) -> None:
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(self.rows)

    def peak(self, key: str) -> int | None:
        values = [r[key] for r in self.rows if r.get(key) is not None]
        return max(values) if values else None

    def peak_of(self, fn) -> int | None:
        """Peak of a per-sample derived value, e.g. anon + shmem."""
        values = []
        for r in self.rows:
            try:
                values.append(fn(r))
            except TypeError:  # a component was missing in this sample
                pass
        return max(values) if values else None
