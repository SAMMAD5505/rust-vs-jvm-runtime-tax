#!/usr/bin/env python3
"""Report-only host preflight: evaluates every host-class gate and prints what a
benchmark would see (EC2 identity, cores, storage, cgroups, Docker). Starts no
containers and changes nothing. Always exits 0; read the VERDICT lines.

Example:
    python3 bench/preflight.py --data data
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import harness as h
import hostinfo

ROOT_FREE_WARN_GB = 25


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=h.REPO / "data", help="data directory to check (default: data/)")
    ap.add_argument("--config-file", type=Path, default=h.REPO / "bench" / "configs.yaml")
    ap.add_argument("--allow-spot", action="store_true", help="evaluate gates as if --allow-spot were passed")
    args = ap.parse_args()
    cfg = h.load_yaml(args.config_file)

    facts = hostinfo.collect_facts(args.data if args.data.exists() else None)
    host = hostinfo.host_section()
    ec2 = facts.get("ec2") or {}
    storage = facts.get("data_storage") or {}
    cpuset, source = h.pick_cpuset(None, cfg["container"]["cores"], dry_run=True)
    cpus = hostinfo.parse_cpulist(cpuset)

    def line(k: str, v) -> None:
        print(f"  {k:24s} {v}")

    print("Host")
    line("OS / kernel", f"{host['os_release']} / {host['kernel']}")
    line("CPU", f"{host['cpu_model']} ({host['cpus_logical']} logical, {host['threads_per_core']} threads/core)")
    line("hypervisor flag", facts.get("hypervisor_flag"))
    line("systemd-detect-virt", host.get("systemd_detect_virt"))
    line("cgroup fs", facts.get("cgroup_fs"))
    line("governors", host["cpu_governors"])
    line("turbo/boost", host["boost"])
    line("SMT control / active", f"{host['smt_control']} / {host['smt_active']}")
    line("page-cache drop", "OK" if facts.get("can_drop_caches") else "NOT AVAILABLE")
    print("EC2 (IMDSv2)")
    if ec2:
        for k in ("instance_type", "lifecycle", "instance_id", "region", "availability_zone", "ami_id"):
            line(k, ec2.get(k))
    else:
        line("detected", "no")
    print("Cores")
    line("chosen", f"{cpuset} ({source})")
    for c, sib in hostinfo.thread_siblings(cpus).items():
        line(f"cpu{c} siblings", sib)
    print("Data")
    line("path", storage.get("path") or f"{args.data} (missing)")
    line("device / fs", f"{storage.get('source')} / {storage.get('fstype')} at {storage.get('mountpoint')}")
    line("disk model(s)", storage.get("disk_models"))
    if args.data.exists():
        free = shutil.disk_usage(args.data.resolve()).free
        line("free space", f"{free / 1e9:.1f} GB")
    print("Root disk (Docker data, results/)")
    root_free = shutil.disk_usage("/").free / 1e9
    line("/ free", f"{root_free:.1f} GB")
    if root_free < ROOT_FREE_WARN_GB:
        line("WARNING", f"less than {ROOT_FREE_WARN_GB} GB free on /: use a 60 GB root volume (docs/aws.md) "
                        "or reclaim space with `docker builder prune`")
    docker_root = hostinfo._run("docker", "info", "--format", "{{.DockerRootDir}}") or "/var/lib/docker"
    du = hostinfo._run("sudo", "-n", "du", "-sh", docker_root)  # root-only directory; may be refused
    if du:
        line(docker_root, du.split()[0])
    else:
        line(docker_root, "(not readable without root; Docker's own accounting below)")
        for row in (hostinfo._run("docker", "system", "df", "--format",
                                  "{{.Type}}: {{.Size}} (reclaimable {{.Reclaimable}})") or "").splitlines():
            line("", row)
    print("Docker")
    docker = hostinfo.docker_section(cfg["images"])
    line("server", f"{docker['server_version']} (cgroup {docker['cgroup_version']}, driver {docker['cgroup_driver']})")
    for name, img in docker["images"].items():
        line(f"image {name}", img.get("id") or "MISSING (run `make images`)")

    print("Gates")
    for cls in hostinfo.HOST_CLASSES:
        blockers = hostinfo.evaluate_gates(cls, facts, cfg["hosts"], allow_spot=args.allow_spot,
                                           want_drop=cfg["protocol"]["drop_caches"])
        print(f"  VERDICT {cls:10s} {'PASS' if not blockers else 'FAIL'}")
        for b in blockers:
            print(f"      - {b}")
    if hostinfo.is_smoke_test(facts):
        print("  NOTE: Spot instance; any results are SMOKE TEST only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
