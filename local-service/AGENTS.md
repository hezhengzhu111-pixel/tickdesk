# Local service development

Read README.md and contracts/v2 before changes. In the parent workspace also read docs/design/local-service.md and docs/11-三端接口契约.md.
This is an independent Cargo project. Do not add path dependencies on sibling projects or edit imported tick-stock-panel/im-foundation code without reading their own instructions.
Keep live/ready distinct. Do not advertise unimplemented commands. No brokerage/automatic trading, cloud file storage or guessed account values.
Use SQLite transactions for durable business effects; preserve endpoint identity on ordinary restart. Database restore requires a new epoch and explicit recovery, not automatic replay.
Verify cargo test --locked, cargo fmt --check, cargo clippy --all-targets --locked -- -D warnings. Do not edit contract snapshots directly; update canonical contracts in the parent workspace and export them.
