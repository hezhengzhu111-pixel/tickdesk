# TickDesk

TickDesk 的源码仓库，按三个运行端组织：

| 目录 | 内容 |
|---|---|
| `local-service/` | Rust 本地服务；`local-service/relay/` 为 Rust WSS 中继 |
| `im-service/` | Python/FastAPI 量化与 AI 服务（含前端、MCP 工具和打包脚本） |
| `android-app/` | Kotlin/Jetpack Compose Android App，含 `:core` 与 `:mobile` |

共享的 v2 契约位于 `contracts/`，三端各自的编译期快照保存在各工程的 `contracts/v2/`。`scripts/` 保留契约同步、真实 TLS/WSS 联调和 Android 隔离仪器测试工具；`docs/` 只保留关键设计、部署和验收记录。

## 构建

### 本地服务与中继

```powershell
cd local-service
cargo test --locked
cargo build --locked

cd relay
cargo test --locked
cargo build --locked
```

### IM / 量化服务

```powershell
cd im-service/backend
uv sync
uv run pytest

cd ../frontend
pnpm install
pnpm build
```

### Android

```powershell
cd android-app
.\gradlew.bat :core:test :mobile:assembleDebug :mobile:lintDebug
```

`android-app/local.properties` 由开发者本地创建，不进入仓库。

## 契约

在仓库根目录执行：

```powershell
python contracts/build_v2.py
python contracts/validate.py
python scripts/sync_contracts.py
python scripts/sync_contracts.py --check
```

## 数据与安全

本仓库不包含运行台账、SQLite/Parquet 数据库、原图、备份、APK、Rust `target/`、Android/Gradle 构建目录、Python/Node 依赖目录、证书、密钥或 `.env`。部署时请在仓库之外准备独立的数据目录、配对凭据和证书。
