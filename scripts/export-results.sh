#!/usr/bin/env bash
# Pack results directories (manifest, runs, figures, tables) into one archive
# with a SHA-256 next to it, ready to copy off the instance before it stops.
#
# Usage: scripts/export-results.sh [results/<ts> ...]   (default: newest results dir)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

if (($#)); then
  dirs=("$@")
else
  latest=$(find results -mindepth 1 -maxdepth 1 -type d -name '2*' | sort | tail -n1)
  [[ -n $latest ]] || { echo "export-results: no results/<timestamp> directories" >&2; exit 1; }
  dirs=("$latest")
fi
for d in "${dirs[@]}"; do
  [[ -f $d/manifest.json ]] || { echo "export-results: $d has no manifest.json" >&2; exit 1; }
done

host=$(curl -sf -m 2 -X PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null \
  | xargs -r -I{} curl -sf -m 2 -H 'X-aws-ec2-metadata-token: {}' \
      http://169.254.169.254/latest/meta-data/instance-type 2>/dev/null || true)
host=${host:-$(hostname -s)}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p results/exports
archive="results/exports/rvj-results-${host}-${stamp}.tar.gz"

tar -czf "$archive" "${dirs[@]}"
(cd "$(dirname "$archive")" && sha256sum "$(basename "$archive")" >"$(basename "$archive").sha256")

size=$(du -h "$archive" | cut -f1)
echo "archive: $REPO/$archive ($size)"
echo "sha256:  $(cut -d' ' -f1 "$archive.sha256")"
echo "contents: ${dirs[*]}"
echo "copy it off BEFORE stopping the instance, e.g. from your machine:"
echo "  scp <user>@<instance>:$REPO/$archive{,.sha256} ."
