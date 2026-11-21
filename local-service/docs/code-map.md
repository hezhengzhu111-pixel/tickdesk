# 本地服务代码结构与 AI 接入边界

现行 Python 接入、调度和部署方式见 [本地 Python AI](ai-python.md)。下表记录 2026-11-15 整理验收时的基线。

## 1. 现有职责

| 环节 | 代码 | 语言及职责 |
|---|---|---|
| 启动与配置 | `src/main.rs`、`config.rs`、`lifecycle.rs` | Rust：启动存储、通信、定时任务和供应商适配器，关闭与身份管理 |
| 协议与命令 | `contract.rs`、`commands.rs` | Rust：44 个命令的契约、幂等、业务事务、回执、outbox；Schema 编译器按固定定义名缓存 |
| 台账与规则 | `domain.rs`、`domain/ledger*`、`budget.rs`、`valuation.rs` | Rust：人工成交、基线、可卖量、核对、执行进度、预算和估值；缺证据不猜数值 |
| 行情与量化接入 | `kernel.rs`、`market.rs`、`quant_observations.rs` | Rust：探测、请求捕获、HTTP 调用、版本复核、观察和事件持久化；不在本轮搬迁量化算法 |
| 量化计算 | `../tick-stock-panel/backend/app/api/tickdesk.py`，同一 Python 后端的 `services/tickdesk_adapter.py`、`tickdesk_worker.py`、`tickdesk_research.py` 等 | Python：现有数据准备、受限策略计算、财报历史和量化输出；其他策略模块沿用现有后端 |
| 事项和同步 | `domain/tasks.rs`、`domain/snapshot.rs`、`reminders.rs` | Rust：事项状态、冻结同步、版本化 WSS 提醒；续页直接读已保存快照 |
| 聊天（现有） | `chat.rs`、`provider.rs` | Rust：持久受理、只读证据、模型请求、校验和消息发布；证据读取最多 20 条、记录预算 24 KiB |
| 图片识别（现有） | `vision.rs`、`vision/imports.rs`、`provider.rs` | Rust：告知授权、识别任务、模型调用、草稿、预览与人工确认；确认导入才改变台账 |
| 存储与恢复 | `storage.rs`、`storage_*.sql`、`backup.rs`、`domain/recovery.rs` | Rust/SQL：SQLite、历史迁移、备份、恢复纪元及人工对账 |
| 本地原图 | `file_service.rs`、`download.rs`、`deletion.rs` | Rust：上传校验、文件租约、下载、手动删除与墓碑；原图留在本地 |
| 网络通信 | `relay.rs`、`http.rs`、`transport.rs` | Rust：WSS 中继、健康与本机管理端点、通用有界 HTTP 读取和敏感凭据读取 |

表中路径除明确标注外，均相对 `local-service`。`transport.rs` 是通用 I/O，量化也在使用，未来不能随 AI 迁移一起删除。

## 2. 现有链路

1. App 命令经云中继送到 `relay`，持久收妥。
2. `commands` 核验身份、纪元、请求摘要和幂等；在 SQLite 事务内分发业务。
3. 普通业务在本地提交。需要扫描时先捕获版本和输入，再在事务外调用 Python `POST /api/tickdesk/prepare`。
4. 返回后复核纪元、扫描租约、控制和持仓版本等，持久化可接受的量化观察与业务结果。
5. 业务记录、回执和 outbox 保持原子性，通信层向 App 同步。研究观察当前另有 loopback `GET /quant/observations/{scanId}`。
6. 聊天和图片识别使用异步工作流程，网络调用不持有存储锁；识别结果必须经过预览和确认才进入台账。

当前没有 `get_analysis_context` 统一 AI 事实入口，也没有完整的外网事实采集、五分钟采集计划与综合分析流水线。这些是下一阶段任务。

## 3. 本轮已经删除或优化的内容

- 删除 `control()` 内不可达的 resume 后备分支；正式 `resume` 仍由 `market::resume` 处理。暂停只读取所需控制版本。
- outbox 规范化 JSON 只生成一次，限长和 SHA-256 共用同一份字节。
- 消息、日志、事项操作及恢复记录的分页初始装载改为闭包；已有游标不重装当前列表。恢复汇总计数仍读取当前证据，这是正确性需要。
- 聊天证据按 SQLite 游标读取；达到数量上限后停止读取和解析其余记录，仍标明截断并保留原排序与字节筛选。
- 三处 HTTP 响应限长合并到 `transport.rs`；有无 Content-Length 均有限制，各调用方保持自己的错误分类。
- AI 与量化内核凭据统一为限长读取及敏感 Bearer 头。错误不带密钥内容或文件路径。
- 单定义 JSON Schema 改为固定集合、按需编译一次的并发缓存，仍验证格式和嵌套引用，不缓存用户输入是否有效。
- README 修正命令数和提醒说明，历史开发记录明确标注时效。

## 4. 为什么这些没有删除

现有 `chat.rs`、`vision.rs` 和 `provider.rs` 都有启动入口、命令调用及回归测试。直接删除会使聊天和图片导入中断。后续只能在 Python 替代链路通过后删除其中的模型处理实现。

`storage_*.sql` 是旧库升级路径；`http::router()` 仍供无存储健康检查测试使用；`integration-fixtures` 仍供显式隔离联调使用；协议中的历史能力字段承担兼容职责。这些不是因为文件旧就能删除的代码。Cargo 依赖没有发现可证明无调用且可安全删除的项，本轮不凭猜测移除依赖。

本轮不删除用户数据库、截图、备份、凭据、运行日志或其他工程。工作区无 Git 元数据，修改前源码及哈希保存在根目录 `.superpowers/sdd/2026-11-15-local-service-cleanup/before` 和 `before-hashes.json`。

## 5. 下一阶段 AI 边界

| Python 负责 | Rust 继续负责 |
|---|---|
| 外部搜索抓取、事实清洗与去重、分类频率、AI 触发与调度、上下文组装、模型适配、综合分析、输出语义验证、多轮对话、OCR 与图片内容识别 | 通信鉴权、身份和纪元、业务存储事务、账户与成交台账、现有交易规则和预算、原图存取授权、确认导入、恢复与可靠 outbox |

Python 不直接写本地服务 SQLite，也不再建立第二套账户或量化状态。新增受控本机读写接口只承载必要的上下文和类型化 AI 记录；AI 结论不能绕过 Rust 硬规则。Rust 为 Python 提供通用存储与提交能力，不继续新增提示词、模型选择或 AI 推理逻辑。

迁移 `chat`、`vision` 时按职责拆分，不能整文件一刀删除；原图租约、预览 CAS、台账和回执事务要保留。新旧实现通过配置逐条链路切换，同一任务只允许一个执行者。

完整执行要求见 [Python AI 开发提示词](../../docs/prompts/ai-python-implementation.md)。量化的真实能力和验收边界继续以 [量化观察契约](../../docs/design/quant-kernel-v3.md)和 [验收记录](../../docs/testing/quant-v3-acceptance.md)为准，不因本轮清理而变更。
