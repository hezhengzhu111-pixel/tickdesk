# TickDesk 2026-11-21 阻断修复与复验记录

范围：单个用户、同版新部署。未做旧 APK 兼容、旧数据迁移、正式策略启用或付费供应商调用。本轮已连接实体手机 `25128PNA1C`（Android 17，序列号 `a1ae5637`），用临时配对/临时 SQLite/独立 Room 完成三端仪器回归；新功能中的恢复表单、复盘钻取和分钟页面仍只到编译/JVM/服务测试，不冒充实机逐项点击。

## 结果总表

| 问题 | 修复前实际值 | 修复后实际值 | 服务与 APK 摘要 | 验收层级 | 尚缺条件 |
|---|---|---|---|---|---|
| 1A 同名不同代码版本的正式退出 | `executable=true, availableQty=600, restriction=null`；分区只按 `strategyId` 匹配 | `executable=false, availableQty=null, restriction=strategy_origin_unverified`；无通知；持仓事实仍保留 600 可卖 | `tickdesk-local.exe` `F9BEE476…A048277`；手机普通 APK `C8CB1C86…430038` | Rust 真实命令/事务/临时 SQLite | 未在新 APK 上点开该事件 |
| 1B 可卖 → 全冻 → 解冻 → 部分成交 → 归零 | 全冻后 `get_holding_details.availableQty=0`，但事件与 `get_task` 仍为 `true/600`，扫描命中 `FORMAL_EXIT_LATCHED` | 事件同事务改为 `false/0/frozen`，旧卡 `expired` 且清空当前提醒；解冻 250 后仅真实阶段转换新建一次任务并提醒一次；部分卖出后 `sellable_unknown/null`；归零后事件 `resolved`；跨重启保持；提醒总数 2 | 同上 | Rust 集成测试，含一次 `drop`/`Storage::open` 重启 | 未做实体锁屏通知计数 |
| 2 恢复“已包含”表单 | JSON 数字经 `.text()` 读为 `null`，`!!` 在发请求前失败；`availableQty` 缺失被补 0，`asOf` 缺失被补当前时间 | `:core` 类型化构造器：数字 `3/600/250/1000000` 正确；缺字段返回字段错误；未知可卖为 `null`；成本 `0` 合法；不再自动采用第一条持仓 | 手机普通 APK `C8CB1C86…430038` | JVM 核心测试 6 条 + 编译 + Lint + 手机 `RecoveryFlowTest`/`RoomRecoveryTest` | 未在新 APK 上逐项点击五种恢复决定 |
| 2 备份早于确定性拒绝回执 | 真实命令与真实拒绝 ACK 均 `verified`，服务给出 `discard_intent`，提交仍 `RECOVERY_EVIDENCE_INCOMPLETE`；判据只查恢复库 `command_results` | 恢复库没有旧 ACK 时，要求同一恢复项内“已验证原命令摘要 + 同 requestHash 的已验证确定性拒绝 ACK”；成功/`not_found`/缺 ACK 仍拒绝。真实备份→拒绝→恢复测试返回 `ok=true, remainingCount=0` | 同上 | Rust 真实 `backup_to`/`restore_from` + 真实 `record_execution` ACK | 未用新 APK 上传同一批证据 |
| 3 分钟行情请求时点 | 09:30:30 查 1m 返回全部 8 根，其中 7 根尚未开始；09:31–09:39 的 5m 只生成 09:35 桶，09:31–09:34 被吞 | 先按证券、交易日、交易段、`startAt<=cutoff`、可得时点过滤；1m 只余真正已开始的 09:30 未完结根；5m 生成 09:35 完整桶，并为 09:30 返回 `missing_first_minute` 缺口（观测 4 分钟，不借 09:31 开盘价） | Python→Rust 契约已同步 | Python 14 条分钟测试 + Rust `get_chart` 校验测试 | 未接真实分钟授权，未走 TLS 到 APK 截图 |
| 3 缺成分/金额/晚到修订 | 缺桶首静默丢弃；缺一个成分金额可能仍标完整 | 缺中间/末根保持 `incomplete_group`；缺金额为 `incomplete_amount`、成交额 `null`、不标完整；收盘后修订使旧游标失效；周末与未来可得行被拒 | 契约新增 `ChartGap` 与 `incomplete_amount` | Python + Rust | 同上 |
| 4 单样本 201 笔买入证据 | `samples(limit=1)` 等待回执超时；样本内嵌最多 200 个 ID，201 时整页超限 | 样本摘要返回 `buyExecutionCount=201`、首批 50 个 ID、下一页游标；逐页 50/50/50/50/1 共 201 个唯一 ID；199/200/201 均通过；190/190/300 大页回归仍通过 | 契约样本上限 50，新增 `buyExecutionCount`/子清单游标 | Rust 真实 `record_execution` 201 笔 + 契约测试；真实 WSS R06 大页 | 未在实体 APK 上点开 201 个 ID |
| 4 复盘空态与钻取 | 所有视图都用 `items.isEmpty()` 判空；有样本/排除仍提示“没有……”；ID 只是文本 | 按当前视图数组判空；游标提示改为“所选视图续页”；游标失败页提供“重新查询第一页”；买卖 ID 可点开详情，显示原始价量、时间、费用、状态、事项与配对证据，快照无该成交时明确“不可用” | 手机普通 APK `C8CB1C86…430038` | Compose 编译 + Lint | 未做真机“跨页→点开→返回→重启”路径 |
| 5 首次同步/进度冲突 | 冷启动可能先排队持仓读取再等首次完整快照；进度冲突测试已在服务端存在 | 持仓读取以 `synced && epoch != null` 为条件，未同步时明确显示“等待首次完整同步”；`PROGRESS_VERSION_CONFLICT` 测试继续通过 | 同上 | Rust 进度测试 + Compose 编译 | 未能在新 AVD 复跑 R02 设备路径 |
| 5 存储/备份状态 | 设置页只有目录尺寸手工读数，不是服务功能 | 新增 `get_storage_status`：原图库、隔离区、暂存、可用空间、最近成功备份；设置页区分读取中/失败/未知/成功，不显示绝对路径 | 契约命令 55 条 | Rust 真实目录测试 + Android 编译 | 未做实体设置页点击 |
| 5 长证据与空态以外的复盘 | 空态/提示矛盾 | 已按视图数组与真实游标状态修正 | 同上 | 编译、Lint | 未做 TalkBack/字号/键盘 |

## 实际运行的验证

- 契约：`build_v2.py` → `validate.py` → `sync_contracts.py` → `--check`。结果：55 条命令、191 个业务样例、11 个 relay 样例；三端快照一致。
- 本地服务：`cargo test --locked` 全部通过（230+ 条，含新增 1A/1B、恢复拒绝、201 证据、存储状态）；`cargo fmt --check`、`cargo clippy --all-targets --locked -- -D warnings` 通过。
- relay：`cargo test --locked` 全部通过；`cargo fmt --check`、Clippy 通过。
- Python：`test_tickdesk_adapter.py`、`test_tickdesk_chart_minute.py`、`test_tickdesk_chart_periods.py`、`test_tickdesk_v3_scan.py`、`test_tickdesk_v3_decisions.py` 共 77 通过。
- Android：`:core:test`、`:mobile:assembleDebug`、`:mobile:lintDebug`、`:mobile:assembleDebugAndroidTest` 通过；普通 APK 已重新构建。
- 实体手机 `a1ae5637`（25128PNA1C / Android 17）：`ThreeTierIntegrationTest` `OK (1 test)`，45.556 秒；隔离本地库 1 条持仓、2 条成交；原 USB 映射与已安装配对摘要保持。`RecoveryFlowTest` `OK (1 test)` 1.34 秒；`RoomRecoveryTest` `OK (2 tests)` 0.327 秒。测试后已安装回普通 APK 并冷启动成功。
- 真实 TLS/WSS 隔离联调：`scripts/integration_services.py`、`scripts/integration_ws_reminders.py`、`scripts/verify-local-ai.py` 全部通过（AI 使用环回供应商）。
- 新隔离配对（`127.0.0.1:8443`，临时数据目录，未触碰正常台账）：`acceptance_r01.py` 全部 PASS（`-100/-105`、`0.8+1.2` 净 92、区间 60 股等）；`acceptance_r06_large.py` 全部 PASS（190/190/300，跨视图与跨查询游标均拒绝）。

## 本次新增反例（已单独运行）

- `formal_exit_never_uses_a_partition_from_a_different_code_version`
- `formal_exit_rechecks_executability_quantity_and_reminders_across_restart`
- `one_refuted_exit_strategy_does_not_release_another_verified_reason`
- `deterministic_rejection_after_the_backup_can_discard_the_old_intent`
- `RecoveryFormTest` 6 条 JVM 用例
- `test_request_cutoff_excludes_minutes_that_have_not_started`
- `test_missing_bucket_start_is_a_visible_gap_not_a_borrowed_open`
- `test_missing_middle_or_last_component_stays_incomplete`
- `test_missing_component_amount_is_unknown_not_zero`
- `test_weekend_and_unavailable_rows_are_rejected`
- `test_late_revision_after_close_invalidates_the_old_cursor`
- `minute_chart_rejects_a_future_start_and_forwards_a_missing_first_gap`
- `one_sample_with_201_buy_evidences_returns_a_bounded_sublist`
- `storage_status_measures_the_authoritative_directories_and_last_successful_backup`

## 同版工件

- `tickdesk-local.exe`：`F9BEE476E81AE610D86FCA3415C4F7AE1347016D08C5E9C7308E2A7E7A048277`
- `tickdesk-relay.exe`：`4484953583D60C5B6D01427A02D5AF500216F5E20CB68F083B74E80D5BBF9A83`
- `mobile-debug.apk`（手机最终普通构建）：`C8CB1C86DC15FEE0C11208BB447F261D1A3945C4A52CD39DBE7C5B5E2F430038`
- 首轮仪器测试因 `POST_NOTIFICATIONS` 未授予失败；授权后重跑。第二次因手机时钟比主机快约 1.1 秒导致 `snapshotAsOf`/`executedAt` 被判未来；仪器测试改用真实时刻减 5 秒，未放宽服务端未来时间校验。两次均不是业务断言失败。
- Android 仪器测试 APK 已编译：`mobile-debug-androidTest.apk`

## 未取得或未声称通过

- 实体手机已跑三端隔离回归与独立 Room 恢复用例；但“新 APK 实际走五种恢复决定”、R02 首次同步与进度冲突设备路径、复盘钻取返回保留页、实体锁屏通知仍未逐项点击取证。
- 没有真实分钟数据授权，分钟验收只到合成仓库与 Python→Rust 契约/校验；未声称真实分钟源逐根一致。实体手机未安装后逐页核对分钟缺口。
- 没有正式审批阈值、真实费用/成交供应商或生产准入，因此未创建正常部署审批；退出用例使用隔离且明示版本的合成规则。
- 未完成同版 24 小时/1–2 周长稳记录；本轮不把构建成功或单次脚本通过写成长期稳定。
