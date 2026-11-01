# Native Rust image: datagen + pipeline-rust (system and mimalloc allocators).
# Build from the repository root: docker build -f docker/rust.Dockerfile -t rvj-rust .

# rust:1.81.0-bookworm
FROM rust@sha256:7b7f7ae5e49819e708369d49925360bde2af4f1962842e75a14af17342f08262 AS build
ARG RUST_VERSION=1.81.0
# Fail the build unless the toolchain is exactly the pinned one; record it for the
# runtime image (which has no rustc) so the harness can read it at bench start.
RUN rustc --version && cargo --version \
 && rustc --version | grep -q "^rustc ${RUST_VERSION} " \
 && cargo --version | grep -q "^cargo ${RUST_VERSION} " \
 && { rustc -vV; cargo -vV; } > /toolchain.txt
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY .cargo .cargo
COPY datagen datagen
COPY pipeline-rust pipeline-rust
RUN cargo build --release --locked --workspace \
 && cargo build --release --locked -p pipeline-rust --no-default-features --features mimalloc \
      --target-dir target/mimalloc

# debian:bookworm-slim
FROM debian@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8
COPY --from=build /src/target/release/datagen /usr/local/bin/datagen
COPY --from=build /src/target/release/pipeline-rust /usr/local/bin/pipeline-rust
COPY --from=build /src/target/mimalloc/release/pipeline-rust /usr/local/bin/pipeline-rust-mimalloc
COPY --from=build /toolchain.txt /usr/local/share/rvj/toolchain.txt
COPY docker/run-wrapper.sh /usr/local/bin/run-wrapper
RUN chmod 0755 /usr/local/bin/run-wrapper
LABEL org.opencontainers.image.source="https://github.com/manojkumargoudsowdari/rust-vs-jvm-runtime-tax" \
      rvj.rustc="1.81.0"
ENTRYPOINT ["/usr/local/bin/run-wrapper"]
