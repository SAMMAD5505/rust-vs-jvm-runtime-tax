# Correctness targets use the native toolchain (rustc 1.81.0, JDK 21.0.4, sbt 1.10.2,
# Python 3.12). Benchmark targets use the Docker images and run on a qualifying host:
#   HOST_CLASS=bare-metal (default)  Linux bare metal, cgroup v2
#   HOST_CLASS=cloud-vm              allowlisted AWS EC2 instance, data on instance store
# bench/*.py refuse to record timings otherwise (unless DRY_RUN=1). See docs/aws.md.

PRESET ?= dev
DATA ?= data/$(PRESET)
WARMUP_DATA ?= data/small
OUT ?= out/$(PRESET)
# Prefer the venv that scripts/ec2-setup.sh creates.
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
SCALA_JAR := pipeline-scala/target/scala-3.5.0/pipeline-scala.jar
JVM_HEAP := -Xms3G -Xmx3G
HOST_CLASS ?= bare-metal
BENCH_ARGS ?=
HOST_ARGS = --host-class $(HOST_CLASS) $(if $(DRY_RUN),--dry-run) $(if $(ALLOW_SPOT),--allow-spot)
RESULTS ?= $(lastword $(sort $(filter-out %-calibrate %-calibrate-dryrun %-calibrate-smoke \
	%-validate %-validate-dryrun %-validate-smoke,$(wildcard results/2*))))
VALIDATION ?=

.PHONY: build build-rust build-rust-mimalloc build-scala datagen run verify test test-python \
        images calibrate bench heapsweep heapsweep-validate plot compare-hosts \
        aws-preflight export-results clean

# --------------------------------------------------------------- native build + correctness

build: build-rust build-rust-mimalloc build-scala

build-rust:
	cargo build --release --locked --workspace

# Separate target dir so the mimalloc binary does not overwrite the system one.
build-rust-mimalloc:
	cargo build --release --locked -p pipeline-rust --no-default-features --features mimalloc \
		--target-dir target/mimalloc

build-scala:
	cd pipeline-scala && sbt -batch test assembly

# medium (~100 GB) and full (~1 TB) need --yes. Without a Rust toolchain (e.g. on EC2)
# datagen runs from the rvj-rust image.
DATAGEN_YES = $(if $(filter medium full,$(PRESET)),--yes)
ifneq ($(shell command -v cargo 2>/dev/null),)
datagen: build-rust
	target/release/datagen --preset $(PRESET) --out $(DATA) $(DATAGEN_YES)
else
datagen:
	mkdir -p $(DATA)
	docker run --rm --user "$$(id -u):$$(id -g)" -v "$$(readlink -f $(DATA)):/out" --entrypoint datagen \
		rvj-rust:latest --preset $(PRESET) --out /out $(DATAGEN_YES)
endif

run: build
	mkdir -p $(OUT)
	target/release/pipeline-rust --input $(DATA) --output $(OUT)/rust.parquet \
		--summary $(OUT)/rust.json --batch-log $(OUT)/rust-batches.csv
	java $(JVM_HEAP) -XX:+UseG1GC -jar $(SCALA_JAR) --input $(DATA) --output $(OUT)/scala.parquet \
		--summary $(OUT)/scala.json --batch-log $(OUT)/scala-batches.csv

verify: run
	$(PYTHON) bench/verify.py $(OUT)/rust.parquet $(OUT)/scala.parquet \
		--summary $(OUT)/rust.json $(OUT)/scala.json --input-dir $(DATA)

test-python:
	$(PYTHON) -m unittest discover -s bench/tests -v

test: build test-python
	cargo test --release --locked --workspace
	PRESET=$(PRESET) PYTHON=$(PYTHON) tests/integration.sh

# --------------------------------------------------------------- benchmark (qualifying host)

images:
	docker build -f docker/rust.Dockerfile -t rvj-rust:latest .
	docker build -f docker/scala.Dockerfile -t rvj-scala:latest .

# Report-only: every host-class gate, EC2 identity, cores, /data device, Docker. Changes nothing.
aws-preflight:
	$(PYTHON) bench/preflight.py --data data $(if $(ALLOW_SPOT),--allow-spot)

# Needs root, or the sudoers rule for `sysctl vm.drop_caches=3` (scripts/ec2-setup.sh).
bench: images
	$(PYTHON) bench/run.py --data $(DATA) --warmup-data $(WARMUP_DATA) $(HOST_ARGS) $(BENCH_ARGS)

# One rust-system + one scala-g1 run on the small preset; prints projected totals.
# Run this first on a new host. It starts nothing long.
calibrate: images
	$(PYTHON) bench/calibrate.py --data data/small $(HOST_ARGS) $(BENCH_ARGS)

# Heap-size sweep (H_min search + multiples of H_min); defaults to the small preset.
HEAPSWEEP_DATA ?= data/small
heapsweep: images
	$(PYTHON) bench/heapsweep.py --data $(HEAPSWEEP_DATA) $(HOST_ARGS) $(BENCH_ARGS)

# Check that the small-preset H_min holds on the production preset (medium): probe H_min
# and 1.25x H_min per collector, 3 attempts each. SWEEP defaults to the latest sweep.
SWEEP ?= $(lastword $(sort $(filter-out %-validate %-validate-dryrun %-validate-smoke,\
	$(wildcard results/*-heapsweep*))))
VALIDATE_DATA ?= data/medium
heapsweep-validate: images
	@test -n "$(SWEEP)" || { echo "no heap-sweep results; run 'make heapsweep' first"; exit 1; }
	$(PYTHON) bench/heapsweep.py --data $(VALIDATE_DATA) --validate-from $(SWEEP) $(HOST_ARGS) $(BENCH_ARGS)

plot:
	@test -n "$(RESULTS)" || { echo "no results/ directory; run 'make bench' first"; exit 1; }
	@case "$(RESULTS)" in \
	  *heapsweep*) $(PYTHON) analysis/heapsweep.py $(RESULTS) $(if $(DRY_RUN),--allow-dry-run) \
	      $(if $(VALIDATION),--validation $(VALIDATION)) ;; \
	  *) $(PYTHON) analysis/analyze.py $(RESULTS) $(if $(DRY_RUN),--allow-dry-run) ;; \
	esac

# VM-vs-metal ratios (same preset on both): make compare-hosts VM=results/<ts> METAL=results/<ts>
compare-hosts:
	@test -n "$(VM)" -a -n "$(METAL)" || { echo "usage: make compare-hosts VM=results/<ts> METAL=results/<ts>"; exit 1; }
	$(PYTHON) analysis/compare_hosts.py --vm $(VM) --metal $(METAL) $(if $(DRY_RUN),--allow-dry-run)

# Archive results (default: newest results/<ts>; or EXPORT="results/<ts> results/<ts>") for scp.
export-results:
	scripts/export-results.sh $(EXPORT)

clean:
	cargo clean
	cd pipeline-scala && sbt -batch clean
	rm -rf out
