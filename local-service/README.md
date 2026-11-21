# TickDesk 本地服务

独立 Rust 2021 工程，使用 Rust 1.95+。本地服务保存命令、回执、人工台账、业务快照和原图。App 与本地服务通过 v2 WSS 收发，云端负责在线中继。

代码职责与后续 Python AI 迁移边界见 [代码结构](docs/code-map.md)；本次整理的范围、测试及限制见 [整理验收](../docs/testing/2026-11-15-local-service-cleanup.md)。

## 启动

从本工程目录运行：

```powershell
cargo run --locked -- --check-config
cargo run --locked -- --config config/dev.toml
```

默认管理接口在 `127.0.0.1:18765`。`/health/live` 用于存活检查，`/health/ready` 按实际依赖状态返回 200/503，并列出已实现命令和未就绪组件。未配置真实供应商时返回 503，不妨碍本地台账与通信。`--check-config` 验证配置和协议校验器，结束时不创建数据库或建立连接。配置内相对路径以配置文件目录为基准。

需要联接已部署的中继时，在自己的本地 TOML 中加入以下结构，以实际中继 IP、配对 ID 和可信 SPKI pin 替换示例值：

```toml
[relay]
relay_url = "wss://192.0.2.1:8443/relay/v2/connect"
pair_id = "replace_with_pair_id"
token_file = "secrets/local-role.token"
pins = ["sha256/<base64-encoded-SPKI-SHA256>"]

[kernel]
address = "127.0.0.1:3018"
# token_file = "secrets/kernel.token" # 内核开启认证时使用
```

`[relay]` 和 `[kernel]` 均可省略。先在可信渠道配置两个不同的 32 字节随机角色令牌，令牌用无填充 base64url 编成 43 字符。中继配置保存各自的 ASCII 令牌 SHA-256 摘要，本地文件只保存 local 角色令牌，并限制为运行账户可读。App 的配对数据仅携带 app 角色令牌。pin 从服务证书公钥的 DER SPKI 计算，受信轮换期可配置两个 pin。客户端验证证书有效期、IP SAN、pin 和 TLS 握手签名后发送 Bearer。

本机内核配置启用存活探测、能力矩阵和生产策略目录查询。认证拒绝会落为诊断状态。成功探测只说明这些入口可读。

```powershell
docker build -t tickdesk-local:v2 .
docker run --rm --name tickdesk-local -v tickdesk-local-data:/var/lib/tickdesk tickdesk-local:v2
docker exec tickdesk-local curl -f http://127.0.0.1:18765/health/live
```

容器管理接口仅监听容器 loopback。持久卷保存 SQLite、原图与本地备份。

## 已实现命令

| 范围 | 命令 |
|---|---|
| 可靠通信和状态 | `get_command_status`、`get_status` |
| 事项与人工台账 | `act`、`ignore`、`snooze`、`get_task`、`get_task_actions`、`record_execution`、`adjust_position` |
| 量化持仓和进度 | `get_holding_details`、`reconcile_holding`、`get_execution_progress`、`set_execution_progress` |
| 设置 | `pause`、`resume`、`set_profile`、`set_settings`、`register_push` |
| 行情与扫描 | `watch`、`unwatch`、`search_symbols`、`get_chart`、`get_quote_status`、`get_history_coverage`、`get_candidates`、`get_quant_observations`、`scan_now` |
| 对话与导入 | `chat`、`accept_vision_notice`、`register_image`、`cancel_import`、`apply_import` |
| 同步与历史 | `sync`、`get_logs`、`get_messages` |
| 原图 | `begin_upload`、`get_upload`、`finalize_upload`、`list_files`、`read_file`、`cancel_transfer`、`delete_file` |
| 恢复 | `get_recovery`、`recovery_evidence`、`recovery_seal_evidence`、`recovery_reconcile`、`recovery_confirm` |

`resume` 持久解除暂停并发起扫描；扫描是否受理按当时输入与门禁返回。`register_push` 保存设备登记和用户偏好，供应商投递仍待接入。历史读取查询本地 SQLite。人工申报采用十进制，超额、倒序、缺基线、规则或交收资料不足的情形进入待对账，未知行情和估值按契约留空。

现有 v2 事项的后续提醒由每秒运行的持久调度器处理，通过现有 WSS 业务 outbox 发送 `reminder`（完整 Task）和 `task_update`。不需要小米账号或 SDK。提醒调度器只读取已存在事项的 `nextReminderAt`；事项由扫描发布事务创建。critical 在创建后第 5 分钟首次后续提醒，此后每 3 分钟；warn 最多两轮，info 不重复。当前窗口最多 60 秒且不超过事项有效期；休眠或离线错过的窗口不补响。这里描述的是现有 v2 提醒实现，不能据此认定 v3 研究观察已经具备新版操作卡和全部提醒规则。

事项、版本、自动操作记录和提醒 outbox 同事务落库；重启保留原 reminderId 和 deliveryId。提醒只推进 version，过期同时推进 actionVersion。暂停、稍后、终态和过期立即使旧提醒不可重试，原事件内容仍作为隔离记录保留。换手机只清除旧窗口，保留后续计划；备份恢复清除旧纪元的窗口与计划。App 的通知权限、夜间模式及前后台展示由 App 控制，`pushEnabled` 仍仅表示尚未接入的厂商推送偏好，不阻止 WSS 业务事实同步。

上传限 PNG/JPEG、20 MiB、40 MP，分块固定 32 KiB。最终归档前核验完整摘要并解码。在有下载或识别租约时删除返回 `FILE_BUSY`。原图删除保留本地墓碑，台账和手机相册原件由各自的生命周期管理。

持仓截图入口使用 `register_image(autoApply=true)`：Rust 取得真实证券主数据后逐行唯一解析，在同一事务中校验并应用截图中的持仓，未出现的持仓保持原样；省略 `autoApply` 仍走原有草稿预览确认。数量必须为正整数、成本非负且必填；可用量缺失保留空值，已填值不得非法或超过总量。自动保存要求数量、成本、用于唯一解析的代码（无代码时为名称）及已填可用量的模型置信度至少为 `0.9`，这些字段的任何 warning/error 均使整批失败；行情等非持仓字段不用于入账。代码已确定时，提供的名称仍须与主数据一致。缺少截图时点按导入登记时间建立当前申报，并在调整审计中明确记录；不推测交收批次和历史成交，未知限制沿用待对账状态。识别期间持仓版本变化、撤回授权、取消、恢复或旧纪元结果均不得覆盖台账。`vision-v2` 告知明确选图直接更新；旧版仅草稿授权不自动升级。手动 `adjust_position` 可附带可选 `name` 保存已选证券的显示名称，不改变证券代码或量化资格。

## 备份和恢复

运行时从北京时间 03:00 起尝试当日备份，每 5 分钟检查，日期目录位于数据目录下的 `backups`，保留最近 14 个自然日内完成的备份。备份包含本地 SQLite、原图清单和所引用的原图。复制并验证完成后才发布清单和目录。现有备份中的旧原图随该备份的保留期存在。

手动备份和恢复使用现有父目录下的全新目标路径。手动备份先停止使用源数据目录的服务。恢复用单独的配置文件，将 `data_dir` 指向一个尚未创建的目录：

```powershell
cargo run --locked -- --config config/dev.toml --backup-to E:/backups/tickdesk-manual
cargo run --locked -- --config config/restore.toml --restore-from E:/backups/tickdesk-manual
cargo run --locked -- --config config/restore.toml
```

恢复校验数据库完整性和备份摘要，创建新 incarnation 与 serverEpoch，隔离旧队列并开启门禁。有原图的备份先把原图放入 `restore-quarantine`。管理员核对备份之后的可用删除记录后，停服并按文件 ID 用 `--keep-restored-original`、`--mark-restored-original-missing` 或 `--delete-restored-original` 逐项处理。原图复核会推进恢复版本并撤销旧封存。

App 走 v2 证据上传、差异核对、清单封存与恢复确认。目标命令状态未知时保持原意图隔离。以独立目录演练恢复，保留原运行目录。

## 验证

```powershell
cargo test --locked
cargo fmt --check
cargo clippy --all-targets --locked -- -D warnings
```

测试使用独立临时目录和本机测试对端，覆盖契约包摘要、44 个命令样例（原 40 个与 4 个量化扩展）的请求摘要、恢复证据向量、事务回滚、进程强杀、WSS 重连、回执丢失、旧实例并发、原图上传下载及备份恢复。三端假数据联调见工作区 `docs/testing/mock-business-flows.md`；真实量化与 AI 效果不属于本轮业务闭环验收。详见 [历史开发记录](docs/development.md) 和 [契约问题](docs/contract-issues.md)。

原 40 个 v2 命令、四个量化持仓和执行进度扩展及 `get_quant_observations` 研究读取均已接线。新客户端使用当前配对及纪元的只读命令往返核验能力。行情、文本和图像适配器可配置，未配置时返回不可用。量化研究观察可通过本地管理接口或 App 只读命令读取，复现步骤见[量化验收](../docs/testing/quant-v3-acceptance.md)。量化准入、操作风险和真实供应商仍需独立验收。

`get_quant_observations` 返回 version 3 的精简观察，使用可选 `scanId`、`cursor`、`limit`（1–20）。省略 scanId 只查最新扫描，最新未产出时返回 `missing`，研究计算失败返回 `failed`，不回退到旧结果。`available` 可为零条，`false` 和分数 `"0"` 均保留。分页绑定 pair、epoch 和原查询（包括省略 scanId），冻结扫描身份和 historical 标记，10 分钟后过期；翻页应保留原查询。恢复期间和旧纪元观察不作为当前研究返回。响应不包含内部 evidence、事件、持仓或操作建议，不改变生产准入。

Python AI 通过 `[assistant]` 启用，接通原有对话、报告和仓位图片识别，另有可选事实采集及定时分析。启动、接口、回滚和验证见 [本地 Python AI](docs/ai-python.md)。旧直连配置 `[chat]` / `[vision]` 仅供迁移回滚。外部地址要求 HTTPS，原图外发必须先完成告知授权。`budget_file` 可指向本机计划风险预算 JSON，缺失时保持无数量观察。

`integration-fixtures` 是显式编译的联调特性：仅对带 `tickdesk-integration-v1` 标记的假数据响应开放非交易时段流程测试。普通构建不含该放行路径；部署服务使用普通构建，测试二进制单独保存。

本工程使用冻结的 `contracts/v2` 快照。工作区根部可用 `python scripts/sync_contracts.py --check` 核对副本；共享契约更新由三端共同处理。
