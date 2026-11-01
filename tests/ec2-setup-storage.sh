#!/usr/bin/env bash
# Exercises the instance-store part of scripts/ec2-setup.sh against loop devices
# inside a privileged Ubuntu 24.04 container (it formats and mounts devices, so
# never run it directly on a host). Checks: format blank disk, idempotent re-run,
# remount without reformat, wiped disk (stop/start) -> reformat + fstab replaced,
# refusal of a foreign filesystem, of the root device, and of a disk with an
# empty partition table.
#
# Usage: tests/ec2-setup-storage.sh        (needs Docker; uses --privileged)
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

docker run --rm --privileged -v "$REPO:/src:ro" ubuntu:24.04 bash -c '
set -euo pipefail
fail() { echo "FAIL: $*" >&2; exit 1; }
mkdir -p /tmp/r && cp -r /src/scripts /src/docker /tmp/r/
S=/tmp/r/scripts/ec2-setup.sh
# Capture output before grepping: grep -q + pipefail would turn SIGPIPE into a failure.
setup() { RVJ_TEST_STORE_DEV=$1 bash "$S" --storage-only; }

truncate -s 256M /tmp/a.img; A=$(losetup -f --show /tmp/a.img)
out=$(setup "$A"); grep -q "creating ext4" <<<"$out" || fail "blank disk was not formatted"
[[ $(findmnt -n -o SOURCE /data) == "$A" ]] || fail "not mounted at /data"
findmnt -n -o OPTIONS /data | grep -q noatime || fail "missing noatime"
u1=$(blkid -o value -s UUID "$A")
grep -q "UUID=$u1 /data ext4 .*nofail" /etc/fstab || fail "fstab entry missing nofail"
echo "ok 1 format + mount + fstab"

out=$(setup "$A"); grep -q "already mounted" <<<"$out" || fail "re-run was not idempotent"
echo "ok 2 idempotent"

umount /data
out=$(setup "$A"); grep -q "creating ext4" <<<"$out" && fail "existing ext4 was reformatted"
[[ $(blkid -o value -s UUID "$A") == "$u1" ]] || fail "UUID changed"
echo "ok 3 remount without reformat"

umount /data; wipefs -aq "$A"
out=$(setup "$A"); grep -q "replacing stale fstab entry" <<<"$out" || fail "stale fstab entry not replaced"
[[ $(grep -c " /data " /etc/fstab) == 1 ]] || fail "duplicate fstab entries"
grep -q "$(blkid -o value -s UUID "$A")" /etc/fstab || fail "fstab UUID does not match the new filesystem"
echo "ok 4 wiped disk -> reformat + fstab updated"

umount /data
truncate -s 64M /tmp/b.img; B=$(losetup -f --show /tmp/b.img); mkfs.ext2 -q "$B"
if setup "$B" 2>/dev/null; then fail "foreign filesystem was accepted"; fi
[[ $(blkid -o value -s TYPE "$B") == ext2 ]] || fail "foreign filesystem was modified"
echo "ok 5 refuses a disk with another filesystem"

if setup /dev/overlay 2>/dev/null; then fail "root device was accepted"; fi
echo "ok 6 refuses the root device"

# A partition table with no partitions: lsblk shows no children, so only the
# PTTYPE guard can catch it.
apt-get -qq update >/dev/null && apt-get -qq install -y fdisk >/dev/null
truncate -s 64M /tmp/c.img; C=$(losetup -f --show /tmp/c.img)
echo "label: gpt" | sfdisk -q "$C"
[[ $(lsblk -n -o NAME "$C" | wc -l) == 1 ]] || fail "test setup: expected no partitions on $C"
if setup "$C" 2>/dev/null; then fail "disk with an empty partition table was accepted"; fi
[[ $(blkid -p -o value -s PTTYPE "$C") == gpt ]] || fail "partition table was modified"
[[ -z $(blkid -p -o value -s TYPE "$C" || true) ]] || fail "a filesystem was created over the partition table"
echo "ok 7 refuses a disk with an empty partition table"
losetup -d "$A" "$B" "$C"
echo "PASS"
'
