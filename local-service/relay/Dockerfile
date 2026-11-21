FROM rust:1.95-bookworm AS build
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY contracts ./contracts
RUN cargo build --release --locked

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home tickdesk
COPY --from=build /build/target/release/tickdesk-relay /usr/local/bin/tickdesk-relay
COPY config/tls.example.toml /etc/tickdesk/relay.toml
USER 10001:10001
EXPOSE 8443/tcp
ENV TICKDESK_HEALTH_URL=https://127.0.0.1:8443/health/ready
HEALTHCHECK --interval=30s --timeout=4s CMD curl --fail --silent --show-error --max-time 3 --noproxy '*' --proto '=https' --cacert /run/secrets/tickdesk/health-ca.pem --connect-to '::127.0.0.1:8443' "$TICKDESK_HEALTH_URL" || exit 1
ENTRYPOINT ["tickdesk-relay"]
CMD ["--config", "/etc/tickdesk/relay.toml"]
