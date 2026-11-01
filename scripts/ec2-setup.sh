#!/usr/bin/env bash
# Idempotent host setup for the benchmark on AWS EC2 (Ubuntu 24.04, c6id.*).
#
# Run from the repository root as the login user (it uses sudo):
#   scripts/ec2-setup.sh
#   DRY_RUN=1 scripts/ec2-setup.sh     # print every change instead of making it
#   scripts/ec2-setup.sh --storage-only  # only (re)create /data, e.g. after a
#                                        # stop/start wiped the instance store
#
# What it does (safe to re-run):
#   1. apt: Docker Engine (official apt repo), python3-venv, git, nvme-cli, jq,
#      linux-tools (if available for this kernel).
#   2. Formats the local NVMe *instance store* as ext4 ONLY if it has no
#      filesystem, mounts it at /data (noatime), adds it to fstab with `nofail`.
#      Refuses to touch the root/EBS device, a partitioned disk, or any disk
#      whose model is not "Amazon EC2 NVMe Instance Storage".
#   3. Adds the user to the docker group.
#   4. /etc/sudoers.d/rvj-dropcaches: passwordless ONLY for
#      `/usr/sbin/sysctl vm.drop_caches=3`.
#   5. Python venv in .venv with bench/ and analysis/ requirements.
#   6. Prints a preflight summary.
#
# No AWS credentials are needed or stored. The repo is private: clone it with a
# read-only SSH deploy key or a fine-grained read-only token (docs/aws.md).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_USER="${RVJ_USER:-${SUDO_USER:-$(id -un)}}"
MOUNT="${RVJ_MOUNT:-/data}"
STORE_MODEL="Amazon EC2 NVMe Instance Storage"
DRY_RUN="${DRY_RUN:-0}"
SUDOERS=/etc/sudoers.d/rvj-dropcaches
# Test hook ONLY (tests use a loop device): skips the instance-store *model* check;
# the root-device and partition guards still apply.
TEST_STORE_DEV="${RVJ_TEST_STORE_DEV:-}"

STORAGE_ONLY=0
for arg in "$@"; do
  case $arg in
    --storage-only) STORAGE_ONLY=1 ;;
    -h|--help) sed -n '2,28p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "ec2-setup: unknown argument $arg" >&2; exit 2 ;;
  esac
done

if [[ $EUID -eq 0 ]]; then ROOT=(); else ROOT=(sudo); fi
if [[ $(id -un) == "$TARGET_USER" ]]; then AS_USER=(); else AS_USER=(sudo -u "$TARGET_USER" -H); fi

log() { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }
die() { printf 'ec2-setup: %s\n' "$*" >&2; exit 1; }
run() {
  if [[ $DRY_RUN == 1 ]]; then printf '   [dry-run]'; printf ' %q' "$@"; printf '\n'; else "$@"; fi
}
as_root() { run "${ROOT[@]}" "$@"; }
as_user() { run "${AS_USER[@]}" "$@"; }
write_root_file() {  # write_root_file PATH MODE CONTENT
  local path=$1 mode=$2 content=$3
  if [[ $DRY_RUN == 1 ]]; then printf '   [dry-run] write %s (mode %s):\n%s\n' "$path" "$mode" "$content"; return; fi
  printf '%s\n' "$content" | "${ROOT[@]}" tee "$path" >/dev/null
  "${ROOT[@]}" chmod "$mode" "$path"
}
append_fstab() {
  if [[ $DRY_RUN == 1 ]]; then
    note "[dry-run] append to /etc/fstab: $1"
  else
    echo "$1" | "${ROOT[@]}" tee -a /etc/fstab >/dev/null
  fi
}

# ---------------------------------------------------------------- 0. platform
log "platform"
# shellcheck source=/dev/null
. /etc/os-release
note "$PRETTY_NAME, kernel $(uname -r), user $TARGET_USER, repo $REPO"
[[ ${VERSION_ID:-} == "24.04" ]] || note "WARNING: tested on Ubuntu 24.04 only"

# ---------------------------------------------------------------- 1. packages
if ((!STORAGE_ONLY)); then
log "packages"
as_root apt-get update -q
as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
  ca-certificates curl gnupg python3-venv git nvme-cli jq util-linux
if ! as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -q linux-tools-common "linux-tools-$(uname -r)"; then
  note "linux-tools not available for kernel $(uname -r); continuing without cpupower/perf"
fi

if dpkg -s docker-ce >/dev/null 2>&1; then
  note "Docker Engine already installed: $(docker --version 2>/dev/null || echo '?')"
else
  log "Docker Engine (official apt repository)"
  as_root install -m 0755 -d /etc/apt/keyrings
  as_root curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  as_root chmod a+r /etc/apt/keyrings/docker.asc
  write_root_file /etc/apt/sources.list.d/docker.list 0644 \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable"
  as_root apt-get update -q
  as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin
fi
as_root systemctl enable --now docker
fi  # !STORAGE_ONLY

# ---------------------------------------------------------------- 2. instance store
log "instance store -> $MOUNT"
root_src=$(findmnt -n -o SOURCE / || true)
root_disk=$(lsblk -no PKNAME "$root_src" 2>/dev/null | head -n1 || true)
[[ -n $root_disk ]] || root_disk=$(basename "${root_src:-unknown}")
note "root filesystem: $root_src (disk $root_disk)"

stores=()
if [[ -n $TEST_STORE_DEV ]]; then
  note "TEST HOOK: RVJ_TEST_STORE_DEV=$TEST_STORE_DEV (model check skipped)"
  stores=("$(basename "$TEST_STORE_DEV")")
else
  for blk in /sys/block/nvme*n*; do
    [[ -e $blk/device/model ]] || continue
    model=$(sed -e 's/[[:space:]]*$//' "$blk/device/model")
    if [[ $model == "$STORE_MODEL" ]]; then stores+=("$(basename "$blk")"); fi
  done
fi
((${#stores[@]})) || die "no NVMe instance-store device found (model \"$STORE_MODEL\"); is this a c6id instance?"
store=${stores[0]}
dev=/dev/$store
note "instance-store devices: ${stores[*]} -> using $dev"
((${#stores[@]} == 1)) || note "NOTE: ${#stores[@]} instance-store disks; only $dev is used (no RAID)"
[[ $store != "$root_disk" ]] || die "refusing: $dev is the root device"
(( $(lsblk -n -o NAME "$dev" | wc -l) == 1 )) || die "refusing: $dev has partitions"

if findmnt -n "$MOUNT" >/dev/null; then
  current=$(findmnt -n -o SOURCE "$MOUNT")
  [[ $(readlink -f "$current") == $(readlink -f "$dev") ]] || die "$MOUNT is already mounted from $current, not $dev"
  note "$dev already mounted at $MOUNT"
else
  # A partition table with no partitions (e.g. a freshly labelled GPT disk) shows
  # no children in lsblk; refuse it too rather than formatting over it.
  if ! pttype=$("${ROOT[@]}" blkid -p -o value -s PTTYPE "$dev"); then pttype=""; fi
  [[ -z $pttype ]] || die "refusing: $dev has a $pttype partition table"
  if ! fstype=$("${ROOT[@]}" blkid -o value -s TYPE "$dev"); then fstype=""; fi
  if [[ -z $fstype ]]; then
    note "$dev has no filesystem: creating ext4"
    as_root mkfs.ext4 -q -F -L rvjdata -E nodiscard "$dev"
  elif [[ $fstype != ext4 ]]; then
    die "refusing: $dev already has a $fstype filesystem"
  else
    note "$dev already formatted ext4"
  fi
  as_root mkdir -p "$MOUNT"
  as_root mount -o noatime "$dev" "$MOUNT"
fi

uuid=$("${ROOT[@]}" blkid -o value -s UUID "$dev" 2>/dev/null || true)
if [[ -n $uuid ]]; then
  entry="UUID=$uuid $MOUNT ext4 defaults,noatime,nofail,x-systemd.device-timeout=10s 0 2"
  if grep -qE "^[^#]*[[:space:]]${MOUNT}[[:space:]]" /etc/fstab; then
    if ! grep -qF "$entry" /etc/fstab; then
      note "replacing stale fstab entry for $MOUNT (instance store is re-created after stop/start)"
      as_root sed -i -E "\\|^[^#]*[[:space:]]${MOUNT}[[:space:]]|d" /etc/fstab
      append_fstab "$entry"
    else
      note "fstab entry present"
    fi
  else
    append_fstab "$entry"
  fi
fi
as_root mkdir -p "$MOUNT/rvj"
as_root chown "$TARGET_USER:$TARGET_USER" "$MOUNT" "$MOUNT/rvj"
if [[ -L $REPO/data ]]; then
  note "$REPO/data -> $(readlink "$REPO/data")"
elif [[ -e $REPO/data ]]; then
  note "WARNING: $REPO/data exists and is not a symlink; leaving it. Datasets must live on $MOUNT."
else
  as_user ln -s "$MOUNT/rvj" "$REPO/data"
fi
if ((STORAGE_ONLY)); then
  note "/data: $(findmnt -n -o SOURCE,FSTYPE,OPTIONS "$MOUNT" 2>/dev/null || echo 'not mounted')"
  note "free:  $(df -h --output=avail "$MOUNT" 2>/dev/null | tail -n1 | tr -d ' ' || echo '?')"
  exit 0
fi

# ---------------------------------------------------------------- 3. docker group
log "docker group"
if id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx docker; then
  note "$TARGET_USER already in docker group"
else
  as_root usermod -aG docker "$TARGET_USER"
  note "log out and back in (or 'newgrp docker') for the group to take effect"
fi

# ---------------------------------------------------------------- 4. sudoers (drop caches only)
log "sudoers: $SUDOERS"
rule="$TARGET_USER ALL=(root) NOPASSWD: /usr/sbin/sysctl vm.drop_caches=3"
# /etc/sudoers.d is not readable by a normal user: test and read it as root.
if "${ROOT[@]}" test -f "$SUDOERS" && "${ROOT[@]}" grep -qxF "$rule" "$SUDOERS"; then
  note "rule present"
else
  tmp=$(mktemp)
  printf '# rust-vs-jvm-runtime-tax: drop the page cache between benchmark runs, nothing else.\n%s\n' "$rule" >"$tmp"
  if [[ $DRY_RUN == 1 ]]; then
    note "[dry-run] would validate with visudo -cf and install: $rule"
  else
    "${ROOT[@]}" visudo -cf "$tmp" >/dev/null || die "generated sudoers rule failed validation"
  fi
  as_root install -m 0440 -o root -g root "$tmp" "$SUDOERS"
  rm -f "$tmp"
fi

# ---------------------------------------------------------------- 5. python venv
log "python venv: $REPO/.venv"
[[ -x $REPO/.venv/bin/python ]] || as_user python3 -m venv "$REPO/.venv"
as_user "$REPO/.venv/bin/pip" install -q --upgrade pip
as_user "$REPO/.venv/bin/pip" install -q -r "$REPO/bench/requirements.txt" -r "$REPO/analysis/requirements.txt"

# ---------------------------------------------------------------- 6. preflight summary
log "preflight summary"
token=$(curl -sf -m 2 -X PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' || true)
imds() { [[ -n $token ]] && curl -sf -m 2 -H "X-aws-ec2-metadata-token: $token" \
  "http://169.254.169.254/latest/meta-data/$1" || echo "?"; }
note "instance type   $(imds instance-type)"
note "lifecycle       $(imds instance-life-cycle)"
note "region / AZ     $(imds placement/region) / $(imds placement/availability-zone)"
note "cores chosen    $(bash "$REPO/docker/pick-cores.sh" 4 2>&1 || true)"
note "/data device    $(findmnt -n -o SOURCE,FSTYPE "$MOUNT" 2>/dev/null || echo 'not mounted')"
note "/data free      $(df -h --output=avail "$MOUNT" 2>/dev/null | tail -n1 | tr -d ' ' || echo '?')"
root_free_gb=$(df -BG --output=avail / | tail -n1 | tr -dc '0-9')
note "/ free          ${root_free_gb} GB (root EBS: Docker data and results/)"
if (( root_free_gb < 25 )); then
  note "WARNING: less than 25 GB free on /. Use a 60 GB root volume (docs/aws.md) or run 'docker builder prune'."
fi
note "/var/lib/docker $("${ROOT[@]}" du -sh /var/lib/docker 2>/dev/null | cut -f1 || echo '?')"
note "cgroup fs       $(stat -fc %T /sys/fs/cgroup)"
note "docker          $("${ROOT[@]}" docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?')"
note "drop caches     $(sudo -n -l /usr/sbin/sysctl vm.drop_caches=3 >/dev/null 2>&1 && echo 'allowed' || echo 'NOT allowed')"
printf '\nNext: log out/in (docker group), then run: make images && make aws-preflight\n'
