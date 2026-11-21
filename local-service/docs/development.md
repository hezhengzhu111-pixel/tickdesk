# 本地服务开发记录

> 本文件按开发阶段保留历史记录。下文的 29 个命令、待实现事项和各轮测试数只对应当时阶段，不代表当前状态。当前入口见 [README](../README.md)、[代码结构](code-map.md)、[量化验收](../../docs/testing/quant-v3-acceptance.md)及 [2026-11-15 整理验收](../../docs/testing/2026-11-15-local-service-cleanup.md)。

设计依据为工作区 docs/design/local-service.md、docs/11-三端接口契约.md、docs/design/cloud-service.md、docs/research/quant-ai-reference.md 和本工程冻结的 contracts/v2。工程基线只有严格配置、SQLite 身份、数据目录锁、健康检查和关闭信号。工作目录没有 Git 元数据，无法核对上游提交号。

| 阶段 | 实现和验收 |
|---|---|
| 协议和事务 | 使用本地冻结 Schema 编译离线校验器；原始字节拒绝重复键、浮点、指数、负零、非法 Unicode、越界整数和超限消息。测试读取共享样例、manifest 和摘要向量。收妥在 inbox 提交后发送；业务、首次完整 ack、结果账本和 outbox 一起提交。故障注入覆盖 outbox 写失败，独立子进程测试在持久 receipt 和业务提交边界强杀。 |
| 中继和隔离 | 单 WSS 管理器，角色 Bearer、IP SAN / SPKI、hello / welcome / presence、Ping/Pong、重连和发送窗口。回执核对完整传输元组。SQLite peer fence 与业务提交串行，隔离旧 inbox/outbox。两个顺序用并发测试覆盖；已隔离意图用新传输包装仍被阻止。内置 WSS 对端验证断线后重发同一 delivery、原 ack 内容和收妥确认。 |
| 业务 | 事务内执行人工成交、持仓基线、事项操作、控制和设置；读查询保留首次内容，业务同步冻结七个集合和 meta 并计算摘要。测试验证 outbox 故障使持仓写入回滚、版本冲突、超额卖出转待核对以及原字面精度。现场行情、适用规则或交易日历缺失时记录真实申报，保留未核验标签与待核对状态。 |
| 原图和恢复 | 上传块持久化、读取续传状态、文件类型/大小/解码检查、同卷归档、下载四块窗口和持久租约、版本检查和人工删除。故障注入覆盖归档或 unlink 后业务提交失败、重启重试、迟到分块、取消、过期、对端换代。备份必须通过 SQLite 和原图摘要校验；恢复在新目录切换身份，分片证据校验和封存后才能对账确认。覆盖缺片拒绝、同批补录与持仓引用、原图隔离和显式复核。 |
| 内核 | 接入真实的本机探测形状，限定为 GET /health、GET /api/settings/capability-matrix 和 GET /api/strategies。测试对端验证参数、认证拒绝、不跟随重定向、响应上限及诊断持久化。探测成功用于依赖诊断。 |

目前有 29 个真实处理命令，完整清单见本工程 README。未知或未接入能力按契约报错或拒绝进入业务处理。普通重启复用身份。恢复准备和对端实例变化均会隔离旧工作，且不会自动重提未知成交。

## 仍需完成

- Tick Stock Panel 现有策略 HTTP 调用各自构建上下文，缺少同轮冻结的证券池、策略和参数指纹、读前后 generation、逐策略有命中 / 无命中 / 失败的结果与来源证据。正式扫描、候选、证券主数据、带口径的图表和关注池写入仍等待这些接口或权威数据。
- 交易日历和历史规则版本、T+1 日切、完整成本/盈亏、估值曲线、预算预留、时点数据和样本外研究验收尚未完成。可安全应用的现有投影限已确认基线下可用量充足的卖出和明确 T+0 且成本能精确表示的买入。缺口转人工对账。
- 正式 AI 事实包、回答和 OCR 供应商接入、导入确认和厂商推送投递尚未完成。现有事项的后续提醒已通过事务调度器和 WSS outbox 接通；真实事项生成仍依赖正式扫描。视觉告知撤销有共享契约冲突，详见 contract-issues.md。本地设备登记和通知偏好已经持久化。
- 中继仍需验证独立的 10 秒 WS 分帧组装期限、真实断网与云重启负载、磁盘配额、跨平台持久化和设备端展示。当前测试覆盖消息大小、心跳、重连与持久状态，未测实际断电。大文件校验和本地备份经同一个存储拥有者串行，尚未做高负载延迟验收。

## 另两端协作

中继端配置成对的角色摘要和带 IP SAN 的证书，核对当前 session 的路由与 receipt 四元组。进行真实部署的 TLS 轮换、限流、断线等验收。

App 端持久化原始命令与 receipt，等待完整业务 ack。对结果未知的意图查询原 epoch、clientMsgId、requestHash；查询用独立的新 ID。实例变化时先同步并核对未知意图。按页校验七集合及 meta 摘要后再替换缓存。原图下载、取消、删除、重新配对和恢复证据需要设备端参与集成验证。

验证用本地测试对端和独立临时数据。没有使用真实中继凭据、模型、行情连接或用户运行数据库。

## 源码参考摘要

以下摘要标识本次只读核对的本机源码。它们不证明外部仓库提交或生产行为。

- backend/app/api/settings.py SHA-256 3ea0000a16296ea479be156f0185cbf049452998fd8dee47af1129f0c839fdec
- backend/app/api/strategy.py SHA-256 ef27d5b863c7d0f27cecd895c9fdaa508c460752ec1dbac65458d168f6b8f6a2
- backend/app/api/kline.py SHA-256 3008942a653601660bcd1e55516082ccb3bad271383a1d60f4c22806a2ffe4cd
- backend/app/services/screener.py SHA-256 1a65942c56457bb24b9fb97076f9024616e64e0b84f3af1e4cbdc5d0347e9fa6
- backend/app/services/api_gateway.py SHA-256 06eee895fad2218d638a4166a53b1539a48263dd0907f1a3afddbdf909136ab2
- backend/app/backtest/evidence.py SHA-256 36dd849f793152d359527cc7f8e5269f0bea19b75fd13126d34ef4952dcb7153
- backend/app/backtest/walkforward.py SHA-256 85fc8a0a6ceb492b355f017e0a92782ddf5f642296a04d4e78716d691f726a62
- backend/app/services/financial_sync.py SHA-256 3b8ea2dc570d88080aac1fedb5d65b80e74cbdf5ea21aab601c0db9ffd22ceb2

## 验收命令结果

Windows 上使用 Rust 1.95.0 执行。cargo test --locked 通过 48 项测试，cargo fmt --check 及 cargo clippy --all-targets --locked -- -D warnings 均通过。工作区三份 v2 契约快照比较通过。cargo run --locked -- --check-config 通过，未启动数据库和连接。

## 2026-11-15 静态复核与真实relay联调

修复恢复非成交差异误补录/无法关闭、持仓调整恢复需要显式基线、未提交归档原图在取消/过期后的清理。用户确认旧非成交操作人工归档并保留当前恢复状态，新增archive_operation；三端契约快照已同步，App页面需据此实现。回归先失败后通过，当前51项测试及fmt/clippy/build通过。实际relay和local二进制的最终完整联调连续3轮通过，覆盖重启、幂等、原图传输/删除和备份恢复；详细证据与一次未复现握手超时见工作区 docs/testing/2026-11-15-services-review-and-integration.md。未做Android或本轮镜像验收。

## 2026-11-15 现有事项的 WSS 提醒

按用户调整后的范围，不接入小米 SDK 或 HTTPS 发送，现有 `register_push` 和 `pushEnabled` 保持原语义。新增每秒事务调度器，只处理已有事项；提醒轮次、显示版本、自动操作历史及 WSS outbox 原子提交。分级时序、最多 60 秒窗口、暂停/稍后/终态/到期、重启去重和恢复隔离均有独立临时 SQLite 测试。过时提醒保留原文并标记 quarantined，发送前再核验，避免重连补发旧提醒。换手机保留未来计划，恢复备份清除旧纪元计划。没有新增生产测试事项或扫描实现。

新增 10 项回归中，分级调度/事务回滚/暂停/恢复、旧帧淘汰、换手机及 2048 条过时提醒积压边界均验证过失败后修复。最终 `cargo test --locked` 通过 61 项，`cargo fmt --check`、`cargo clippy --all-targets --locked -- -D warnings`、`cargo build --locked`、`--check-config` 全部通过。一次并行联调占用 Windows exe 导致编译拒绝访问；联调释放进程后原命令重跑通过。本子任务未改动或重启真实数据目录。后台连接、通知权限、锁屏与强杀后的设备可达性由 App 集成验证，不由服务端单测证明。
