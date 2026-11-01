"""Unit tests for the host-class gates and /proc parsers (stdlib unittest).

Run: python3 -m unittest discover -s bench/tests -v
"""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hostinfo  # noqa: E402

POLICY = {
    "bare_metal": {"runs": 10},
    "cloud_vm": {"runs": 20, "instance_allowlist": ["c6id.4xlarge", "c6id.metal"],
                 "instance_store_model": "Amazon EC2 NVMe Instance Storage"},
}
INSTANCE_STORE = {"path": "/data/rvj", "source": "/dev/nvme1n1", "fstype": "ext4", "mountpoint": "/data",
                  "device": "nvme1n1", "disk_models": ["Amazon EC2 NVMe Instance Storage"]}
EBS = {**INSTANCE_STORE, "source": "/dev/nvme0n1p1", "device": "nvme0n1p1", "mountpoint": "/",
       "disk_models": ["Amazon Elastic Block Store"]}


def ec2(instance_type="c6id.4xlarge", lifecycle="on-demand"):
    return {"instance_type": instance_type, "instance_id": "i-0123", "region": "us-east-1",
            "availability_zone": "us-east-1a", "ami_id": "ami-0abc", "lifecycle": lifecycle}


# c6id.4xlarge: a Nitro VM (hypervisor flag set) with local NVMe instance store.
VM = {"os": "Linux", "cgroup_fs": "cgroup2fs", "hypervisor_flag": True, "can_drop_caches": True,
      "ec2": ec2(), "data_storage": INSTANCE_STORE}
# c6id.metal: bare metal (no hypervisor flag), still reachable through IMDS.
METAL = {**VM, "hypervisor_flag": False, "ec2": ec2("c6id.metal")}
# A non-EC2 bare-metal box (the original design target).
LAB = {"os": "Linux", "cgroup_fs": "cgroup2fs", "hypervisor_flag": False, "can_drop_caches": True,
       "ec2": None, "data_storage": {"path": "/srv/data", "disk_models": ["Samsung SSD 990 PRO 2TB"]}}


def gates(cls, facts, **kw):
    return hostinfo.evaluate_gates(cls, facts, POLICY, **kw)


def with_(facts, **changes):
    f = copy.deepcopy(facts)
    f.update(changes)
    return f


class BareMetalGate(unittest.TestCase):
    def test_c6id_metal_passes_as_bare_metal(self):
        self.assertEqual(gates("bare-metal", METAL), [])

    def test_non_ec2_lab_machine_passes(self):
        self.assertEqual(gates("bare-metal", LAB), [])

    def test_vm_refused(self):
        self.assertTrue(any("hypervisor" in r for r in gates("bare-metal", VM)))

    def test_spot_refused_even_on_bare_metal(self):
        f = with_(METAL, ec2=ec2("c6id.metal", "spot"))
        self.assertTrue(any("Spot" in r for r in gates("bare-metal", f)))
        self.assertEqual(gates("bare-metal", f, allow_spot=True), [])


class CloudVmGate(unittest.TestCase):
    def test_c6id_4xlarge_on_demand_passes(self):
        self.assertEqual(gates("cloud-vm", VM), [])

    def test_c6id_metal_also_passes_as_cloud_vm(self):
        self.assertEqual(gates("cloud-vm", METAL), [])

    def test_spot_needs_allow_spot(self):
        f = with_(VM, ec2=ec2(lifecycle="spot"))
        self.assertTrue(any("Spot" in r for r in gates("cloud-vm", f)))
        self.assertEqual(gates("cloud-vm", f, allow_spot=True), [])
        self.assertTrue(hostinfo.is_smoke_test(f))
        self.assertFalse(hostinfo.is_smoke_test(VM))

    def test_instance_type_not_allowlisted(self):
        f = with_(VM, ec2=ec2("m5.4xlarge"))
        self.assertTrue(any("allowlist" in r for r in gates("cloud-vm", f)))

    def test_no_ec2_refused(self):
        f = with_(VM, ec2=None)
        self.assertTrue(any("IMDSv2" in r for r in gates("cloud-vm", f)))

    def test_data_on_ebs_refused(self):
        f = with_(VM, data_storage=EBS)
        self.assertTrue(any("instance store" in r for r in gates("cloud-vm", f)))

    def test_data_on_mixed_raid_refused(self):
        f = with_(VM, data_storage={**INSTANCE_STORE, "disk_models": [
            "Amazon EC2 NVMe Instance Storage", "Amazon Elastic Block Store"]})
        self.assertTrue(any("instance store" in r for r in gates("cloud-vm", f)))

    def test_unknown_data_device_refused(self):
        f = with_(VM, data_storage={"path": "/x", "disk_models": []})
        self.assertTrue(any("instance store" in r for r in gates("cloud-vm", f)))


class CommonGates(unittest.TestCase):
    def test_cgroup_v1_refused(self):
        for cls in hostinfo.HOST_CLASSES:
            self.assertTrue(any("cgroup v2" in r for r in gates(cls, with_(METAL, cgroup_fs="tmpfs"))))

    def test_no_cache_drop_refused(self):
        for cls in hostinfo.HOST_CLASSES:
            self.assertTrue(any("page cache" in r for r in gates(cls, with_(METAL, can_drop_caches=False))))
        self.assertEqual(gates("bare-metal", with_(METAL, can_drop_caches=False), want_drop=False), [])

    def test_non_linux_refused(self):
        self.assertTrue(gates("cloud-vm", with_(VM, os="Windows"))[0].startswith("host OS is Windows"))

    def test_unknown_class(self):
        self.assertIn("unknown host class", gates("laptop", VM)[0])


class ProcParsers(unittest.TestCase):
    STAT = ("cpu  100 0 50 900 5 0 1 40 0 0\n"
            "cpu0 25 0 10 200 1 0 0 10 0 0\n"
            "cpu1 25 0 10 200 1 0 0 7 0 0\n"
            "cpu2 25 0 15 250 2 0 1 20 0 0\n"
            "cpu3 25 0 15 250 1 0 0 3 0 0\n"
            "intr 12345\n")
    CPUINFO = ("processor\t: 0\ncpu MHz\t\t: 3500.000\n\nprocessor\t: 1\ncpu MHz\t\t: 2900.000\n\n"
               "processor\t: 2\ncpu MHz\t\t: 3100.500\n")

    def test_steal_all_and_selected(self):
        self.assertEqual(hostinfo.steal_ticks(None, self.STAT), 40)
        self.assertEqual(hostinfo.steal_ticks([0, 2], self.STAT), 30)

    def test_cpu_mhz(self):
        self.assertAlmostEqual(hostinfo.cpu_mhz(None, self.CPUINFO), (3500 + 2900 + 3100.5) / 3)
        self.assertAlmostEqual(hostinfo.cpu_mhz([1], self.CPUINFO), 2900.0)

    def test_parse_cpulist(self):
        self.assertEqual(hostinfo.parse_cpulist("1,3,5-7"), [1, 3, 5, 6, 7])
        self.assertEqual(hostinfo.parse_cpulist(None), [])


if __name__ == "__main__":
    unittest.main()
