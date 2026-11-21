# TickDesk repository layout

- `local-service/` is the Rust local service. `local-service/relay/` is the independent Rust WSS relay.
- `im-service/` is the Python/FastAPI quant and AI service. Read `im-service/CONTRIBUTING.md` before changing it.
- `android-app/` contains the Kotlin core and Android UI. Read `android-app/AGENTS.md` before changing it.
- `contracts/` is the canonical v2 contract source. Update the generator, then run `scripts/sync_contracts.py`; never hand-edit project snapshots.
- Never commit `data/`, databases, originals, backups, logs, certificates, keys, `.env`, APKs, `target/`, `build/`, `node_modules/` or `.venv/`.
