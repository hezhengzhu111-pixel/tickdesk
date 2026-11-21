# Relay development

Read README.md and contracts/v2. In the parent workspace read docs/design/cloud-service.md and docs/11-三端接口契约.md.
No business database, message history or file persistence. Never treat socket writes as endpoint receipts or business acknowledgements.
Plaintext binds only to loopback. Real exposed listeners require TLS. Never log bearer tokens, QR data or message payloads.
WS role auth, fencing and bounded routing are implemented. Keep readiness tied to valid paired roles and a functioning listener/router; a disconnected peer does not make the relay unready. Do not bypass these checks to make smoke tests green.
Verify cargo test --locked, cargo fmt --check and cargo clippy --all-targets --locked -- -D warnings. Keep this Cargo project independently buildable.
