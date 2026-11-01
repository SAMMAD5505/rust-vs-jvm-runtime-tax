"""Convert `jfr print --json` output into GC pause, collection and heap CSVs."""

from __future__ import annotations

import calendar
import csv
import json
import re
from pathlib import Path

EVENTS = "jdk.GarbageCollection,jdk.GCPhasePause,jdk.GCHeapSummary,jdk.JVMInformation,jdk.ZAllocationStall"

_DURATION = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)(?:\.(\d+))?S)?$")
_INSTANT = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d+))?Z$")


def duration_ns(text: str | None) -> int | None:
    """ISO-8601 duration as printed by JFR (e.g. 'PT0.029754776S') -> nanoseconds."""
    if text is None:
        return None
    m = _DURATION.match(text)
    if not m:
        raise ValueError(f"unrecognised JFR duration {text!r}")
    h, mnt, s, frac = m.groups()
    ns = ((int(h or 0) * 60 + int(mnt or 0)) * 60 + int(s or 0)) * 1_000_000_000
    return ns + (int((frac or "0").ljust(9, "0")[:9]) if frac else 0)


def instant_ns(text: str) -> int:
    """UTC timestamp with up to nanosecond precision -> epoch nanoseconds."""
    m = _INSTANT.match(text)
    if not m:
        raise ValueError(f"unrecognised JFR timestamp {text!r}")
    y, mo, d, h, mi, s, frac = m.groups()
    secs = calendar.timegm((int(y), int(mo), int(d), int(h), int(mi), int(s)))
    return secs * 1_000_000_000 + int((frac or "0").ljust(9, "0")[:9])


def _write(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)


def convert(json_path: Path, out_dir: Path) -> dict:
    """Write gc_pauses.csv, gc_collections.csv, heap.csv; return per-run GC totals."""
    events = json.loads(json_path.read_text())["recording"]["events"]
    jvm = next((e["values"] for e in events if e["type"] == "jdk.JVMInformation"), None)
    origin = instant_ns(jvm["jvmStartTime"]) if jvm else min(
        instant_ns(e["values"]["startTime"]) for e in events)

    def t_ns(v: dict) -> int:
        return instant_ns(v["startTime"]) - origin

    pauses, collections, heap, stalls = [], [], [], []
    for e in events:
        v = e["values"]
        if e["type"] == "jdk.GCPhasePause":
            pauses.append({"t_ns": t_ns(v), "gc_id": v["gcId"], "name": v["name"],
                           "duration_ns": duration_ns(v["duration"])})
        elif e["type"] == "jdk.GarbageCollection":
            collections.append({"t_ns": t_ns(v), "gc_id": v["gcId"], "name": v["name"], "cause": v["cause"],
                                "duration_ns": duration_ns(v["duration"]),
                                "sum_of_pauses_ns": duration_ns(v.get("sumOfPauses")),
                                "longest_pause_ns": duration_ns(v.get("longestPause"))})
        elif e["type"] == "jdk.GCHeapSummary":
            heap.append({"t_ns": t_ns(v), "gc_id": v["gcId"], "when": v["when"], "heap_used": v["heapUsed"],
                         "heap_committed": v["heapSpace"]["committedSize"]})
        elif e["type"] == "jdk.ZAllocationStall":
            stalls.append(duration_ns(v["duration"]))

    for rows in (pauses, collections, heap):
        rows.sort(key=lambda r: (r["t_ns"], r["gc_id"]))
    _write(out_dir / "gc_pauses.csv", ["t_ns", "gc_id", "name", "duration_ns"], pauses)
    _write(out_dir / "gc_collections.csv",
           ["t_ns", "gc_id", "name", "cause", "duration_ns", "sum_of_pauses_ns", "longest_pause_ns"], collections)
    _write(out_dir / "heap.csv", ["t_ns", "gc_id", "when", "heap_used", "heap_committed"], heap)

    return {
        "gc_count": len(collections),
        "gc_pause_count": len(pauses),
        "gc_pause_total_ns": sum(p["duration_ns"] for p in pauses),
        "gc_pause_max_ns": max((p["duration_ns"] for p in pauses), default=0),
        # Sum of jdk.GarbageCollection durations: whole-cycle time, including the
        # concurrent phases of ZGC/G1 (wall time of the GC cycle, not CPU time).
        "gc_duration_total_ns": sum(c["duration_ns"] or 0 for c in collections),
        "z_alloc_stall_total_ns": sum(stalls),
        "heap_used_max": max((h["heap_used"] for h in heap), default=None),
        "heap_committed_max": max((h["heap_committed"] for h in heap), default=None),
        # jdk.JVMInformation.jvmVersion, e.g. "OpenJDK 64-Bit Server VM (21.0.4+7-LTS) for linux-amd64
        # JRE (21.0.4+7-LTS), built on ..."; jdk_version is the runtime version in the first parentheses
        # (= java.runtime.version), checked for consistency by the analysis.
        "jdk_version": jdk_version(jvm.get("jvmVersion")) if jvm else None,
    }


def jdk_version(jvm_version: str | None) -> str | None:
    m = re.search(r"\(([^)]+)\)", jvm_version or "")
    return m.group(1) if m else None
