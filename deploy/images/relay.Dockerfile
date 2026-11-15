# syntax=docker/dockerfile:1.12
# Data-plane node `nexus-relay`. Build from the repository root:
#   docker build -f deploy/images/relay.Dockerfile -t nexus-relay .
# Base images are pinned by tag and multi-arch index digest; update both together.
ARG RUST_IMAGE=rust:1.97.1-slim-trixie@sha256:8e8cf8f7fd54a2d23d5a743b3a03f56e26b6c774276c33fa0595111704ebb15c
ARG RUNTIME_IMAGE=gcr.io/distroless/cc-debian13:nonroot@sha256:cb87acc574abe719b6e5c547384defa2f7b1f24af9605d3f618cf852495d8be2

FROM ${RUST_IMAGE} AS build
WORKDIR /src
COPY src/relay/Cargo.toml src/relay/Cargo.lock ./
COPY src/relay/src src
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    cargo build --release --locked --bin nexus-relay \
    && install -D -m 0555 target/release/nexus-relay /out/nexus-relay

# glibc, libgcc and CA certificates only; no shell or package manager.
FROM ${RUNTIME_IMAGE}
LABEL org.opencontainers.image.title="nexus-relay" \
      org.opencontainers.image.description="Nexus data-plane relay" \
      org.opencontainers.image.source="https://github.com/herta-network/Nexus"
COPY --from=build /out/nexus-relay /usr/local/bin/nexus-relay
COPY src/relay/licenses /usr/share/doc/nexus-relay/licenses
ENV NEXUS_RELAY_LISTEN=0.0.0.0:8081
USER 65532:65532
EXPOSE 8081 9464
ENTRYPOINT ["/usr/local/bin/nexus-relay"]
