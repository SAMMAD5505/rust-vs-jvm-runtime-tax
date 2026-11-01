# rust-vs-jvm-runtime-tax: rules for Claude Code

Artifact for the IEEE-style paper "Quantifying the Runtime Tax" (Rust vs JVM
Scala), by Manoj Kumar Goud Sowdari and Akshay Kumar Goud Sowdari.
Read PLAN.md, docs/paper-deltas.md and docs/aws.md before changing anything.

## Non-negotiable
- Never fabricate, estimate or hand-edit benchmark numbers. Results come
  only from the harness on a host that passes the gates.
- Rust and Scala outputs must stay bit-identical. Any change to pipeline
  logic must re-run `make verify` on dev and small presets.
- Never weaken or bypass host gates. Dry runs stay watermarked.
- Toolchains are pinned: Temurin 21.0.4+7 (digest-pinned image), rustc
  1.81.0, arrow/parquet 53.4.1. Don't upgrade without being asked.
- Every fact the paper must change goes into docs/paper-deltas.md.
- Never start AWS resources or handle AWS credentials.

## Workflow
- Work on a feature branch; CI must be green (`gh run list`) before a
  fast-forward merge to main. Tag only on main, only when asked.
- Never move a published tag; add a new one instead.
- Run shellcheck, unit tests and tests/ec2-setup-storage.sh before committing.

## Status (update when it changes)
- v0.3.1 (ca3931f): AWS cloud-vm host class, ec2-setup.sh, version pinning
  and in-image version recording. CI green.
- Next: Spot smoke test on c6id.4xlarge, then On-Demand paper runs and a
  c6id.metal validation run (see docs/aws.md). No timed results exist yet.
