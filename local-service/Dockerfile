FROM rust:1.95-bookworm AS build
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY contracts ./contracts
RUN cargo build --release --locked

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home tickdesk \
    && mkdir -p /var/lib/tickdesk && chown tickdesk:tickdesk /var/lib/tickdesk
COPY --from=build /build/target/release/tickdesk-local /usr/local/bin/tickdesk-local
COPY config/docker.toml /etc/tickdesk/local.toml
USER 10001:10001
VOLUME ["/var/lib/tickdesk"]
HEALTHCHECK --interval=30s --timeout=3s CMD curl --fail --silent http://127.0.0.1:18765/health/live || exit 1
ENTRYPOINT ["tickdesk-local"]
CMD ["--config", "/etc/tickdesk/local.toml"]
