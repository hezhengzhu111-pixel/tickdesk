# 本地量化观察契约 v3

Rust 的扫描在现有 `POST /api/tickdesk/prepare` 请求中显式发送 `quantObservationVersion: 3`。Python 仅在收到该版本时附带 `quantObservationBatch` 和可用的 `holdingRuleBatch`。调用方继续按原约定读取 `frozen`、`quotes` 和 `error`。HTTP 仍限本机，输入上限 1 MiB，输出上限 4 MiB。

## 量化观察

| 字段 | 表示 |
|---|---|
| `version` | 整数 `3` |
| `decisionCutoffMs` | Unix 毫秒，等于 Rust 捕获请求时的 `requestedAt` |
| `observations` | 最多 500 条，证券、策略 ID、周期的组合唯一 |
| `symbol`、`strategyId` | 现有 `Symbol` 和 `Id` 格式 |
| `group` | `daily_swing`、`short_term`、`long_term` |
| `timeframe` | `1d`、`1m`、`5m`、`1w`、`1mo`。当前在线生产者支持 `1d` |
| `codeHash`、`parametersHash`、`dependenciesHash`、`inputHash` | 64 字符的小写 SHA-256 |
| `rulesHash` | 可追溯规则的 SHA-256，未取得时为 `null` |
| `admission` | `research`、`causal_checked`、`oos_checked` 或 `shadow`。当前研究生产者返回 `research` |
| `bar` | `status` 为 `complete` 或 `unknown`。已完成要求 `endMs ≤ availableAtMs ≤ decisionCutoffMs`。未知时 `availableAtMs` 为 `null`，`endMs` 可省略或为已知时点 |
| `entry`、`exit` | `{value, reason}`。`true` 表示成立，`false` 表示已评估且不成立，`null` 表示不可计算并附原因 |
| `score` | `{value, reason, scope}`。有值时按 v2 `Decimal` 传输，范围 0 到 100。`scope` 为 `entry_candidates` 或 `eligible_universe` |
| `quote` | `null` 或 `{price, asOf, source}`。正的原始价按 `Decimal` 传输，并校验 `source.asOf` 与 `asOf` 一致 |
| `evidence` | JSON 对象，保存已核实的 bar 来源、修订和决策输入标识 |

完整性证据绑定证券、市场日期、OHLC 等输入摘要和实际可得时间。盘中可复用最近已完成的决策日，所选池内缺乏对应证据的证券保持不可计算。入场、退出和评分三个值独立。字段缺失不能生成有效零分。

研究策略由数据目录内的 `tickdesk_research.json` 显式选择，包含 `version`、`strategies` 和 `symbols`。源码及依赖走现有受限 worker，测试用名单及策略只存在于隔离夹具。目前流转结果用于研究观察。

## 持仓规则证据

`holdingRuleBatch` 独立于策略结果。形状为 `{version: 3, rules: [...], unavailable: [...]}`，`unavailable` 可省略。规则每条包含交易日、证券与渠道绑定的 canonical `RuleSnapshot`（`rule`）、规则实际可得时点 `ruleAvailableAtMs`、日历证据 `calendar` 和 `companyActionsCheckedThrough`。日历保留 `revisionId`、`source`、`availableAtMs`、`coverageStart`、`coverageEnd` 与 `sessions`，每个会话记录有效日期及 `firstExecutionAtMs`。只有真实存在的规则、日历和公司行为核对证据才可支持推算。券商确认的可卖数量仍单独表示。

明确的归档冲突或撤回通过 `unavailable` 按证券和有效日标识，附 `availableAtMs` 和原因。该记录在 Rust 中使旧证据失效。仅时间更新的有效证据可以恢复规则，本轮无响应或缺归档保持未知。

上交所按标的和事件起日发布的明确停牌记录，可以通过 `holdingRuleBatch` 传递独立的交易状态证据。Rust 单独保存这部分证据，按交易日、渠道、来源和可得时间校验。证券仅有停牌证据时，持仓交收、价格限制及公司行为维持各自的核验状态。用户回填与已可得停牌证据相矛盾的实际成交时，保留申报并要求对账。

停牌事项在允许的扫描时段内随规则证据事务提交，可以在生产策略轮次失败时独立发布。事项保留真实规则和来源，使用 `direction=risk` 与 `score=null`。同一事件只初次通知，忽略事项或服务重启不会重开。用户选择稍后时只产生当次提醒。明确复牌会关闭活动事项，较新的明确停牌事件可以重新创建事项。证据缺失时保持此前已核实的风险状态。该流程不生成成交。

## 本地提交和读取

捕获请求绑定服务纪元、扫描租约、控制版本和持仓版本。Rust 在扫描事务里复核这些字段、暂停和期限。批次内任一观察字段非法时，不接受该批次。未接受的批次不写入量化观察集合，合法的旧扫描结果仍可提交。

服务仅在 loopback 管理监听器提供 `GET /quant/observations/{scanId}`，返回 `{scanId, serverEpoch, status, error, batch, events}`。已接受批次使用 `status: available` 和 `error: null`。内核明确报告研究失败时，`status` 为 `failed`，`batch` 为 `null`，`error` 为 `DATA_UNAVAILABLE`。无该轮记录时返回 404。扫描历史中的已接受观察及当轮事件投影在重启后可查询。旧备份的历史记录保留写入时纪元，恢复后产生的活动事件使用新纪元。

同一证券、策略版本、周期及方向共享持续事件。重复决策 bar、迟到的历史 bar、未知值和同一 bar 的历史修订不会结案或重新触发。较新的完整 bar 明确清除条件后可以结案，后续成立产生关联事件。持仓确认清零会在相同台账事务中关闭退出事件，再建仓可产生关联的新事件。持仓复核记录使用实际成交后的数量、可卖依据和明确分配，研究结果中的 `plannedQty` 保持 `null`。

Android 和中继继续使用 v2 业务信封及七集合快照。四个持仓和进度命令使用 canonical 类型及独立回执，能力探针使用真实往返。App 端用户交互需要单独接入。
