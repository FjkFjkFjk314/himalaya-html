FROM rust:1.100-bookworm AS build
WORKDIR /build
COPY Cargo.toml Cargo.lock build.rs ./
COPY src/ src/
RUN cargo build --locked --release --bin himalaya

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 himalaya \
    && useradd --uid 10001 --gid 10001 --home-dir /var/lib/himalaya --create-home himalaya
COPY --from=build /build/target/release/himalaya /usr/local/bin/himalaya
ENV HOME=/var/lib/himalaya
WORKDIR /var/lib/himalaya
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/himalaya"]
CMD ["--version"]
