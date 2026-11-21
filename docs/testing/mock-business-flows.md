# 三端假数据业务闭环联调

本轮按用户最新要求，把三端流程、数据流转和业务闭环作为验收目标。使用真实 Android App、真实 Rust 中继、真实本地服务；行情、对话和截图识别输入由隔离的固定响应供应商提供。量化质量、真实 AI 效果和真实供应商可用性后移验收。

## 运行方式

### 手机上可持续操作的演示环境

手机中的 **TickDesk 联调**（`dev.tickdesk.app.demo`）是独立安装包，每个页面都有“联调演示 · 全部数据为模拟数据”标识。它仍通过真实 WSS 中继与本地服务通信。原 `TickDesk` 的配对、后台提醒偏好和台账保留。

已部署环境可直接运行：

```powershell
./scripts/mock-demo.ps1 -Action Start    # 重启后恢复服务与 USB 端口转发
./scripts/mock-demo.ps1 -Action Status
./scripts/mock-demo.ps1 -Action Install  # 构建、安装、首次配对、填充并验证
./scripts/mock-demo.ps1 -Action Stop     # 仅停止演示服务，保留全部演示数据
```

首次使用依赖已准备的 `data/app-integration/deployment.json`、`data/completion-venv` Python 环境（含 jsonschema）、已编译的两端普通二进制，以及 `data/app-integration/fixture-bin/tickdesk-local.exe`。`Install` 使用本机 Android Studio JBR 与 SDK；构建属性为 `-PtickdeskMockDemo=true`，输出位于 `app/mobile/build/mock-demo`。

演示本地服务监听 `127.0.0.1:18766`，假供应商监听 `127.0.0.1:18767`。通过独立 demo 配对复用中继 `127.0.0.1:8443`，不会替换原 App 的角色令牌。演示业务库、原图及日志持久保存在 `data/app-integration/mock-demo`。只有首次空库初始化会写入种子；以后启动不覆盖用户操作，也不自动重置数据。

手机需保持 USB 连接且电脑上的服务运行。重新插线后执行 `Start` 恢复转发。APK 保存在 `data/app-integration/mock-demo/tickdesk-demo.apk`，仅适用于当前本地联调证书环境。

2026-11-15 已在 USB 手机确认：

- 事项页面显示模拟风险事项和扫描生成的买点，可查看、忽略、稍后或回填成交。
- 持仓页面有模拟平安 500 股、模拟科技 300 股、模拟银行 1000 股；覆盖 3/3，模拟市值 ¥19,225.00。
- 已通过实际命令完成一笔模拟成交及持仓核对，保存成交记录和估值记录。
- 对话有模拟答复，截图原图已上传并保留，`demo_screenshot` 导入已生成待人工确认的差异预览。
- `PersistentDemoTest` 首次填充通过（7.881 秒）；停止并重启演示服务、强制停止 App 后再次验证通过（4.099 秒），数据仍在。
- `mock-demo.ps1 -Action Install` 已整体执行通过；安装后显式前台启动解决 MIUI 冷启动测试暂停问题。最终安装验证为 3.74 秒通过。普通 USB 版与联调版均通过 core 测试、App/测试 APK 构建和 lint。
- 原工作台 incarnation、server epoch 均与更新前一致；未将演示持仓写入原库。升级普通服务前已创建 `backup-before-demo-*` 备份。

截图与证据：`data/app-integration/logs/demo-inbox.png`、`demo-portfolio.png`、`demo-business.log`、`demo-restart.log`。早期 `demo-pairing.log` 保留了一次失败尝试；后续实际页面同步、业务验证与重启验证才是最终结果。

演示供应商固定返回成功扫描，不受真实交易时段影响。事项仍遵守有效期；过期后通过“选股 → 立即扫描”生成新一轮结果。演示识别仅返回预设的三条模拟持仓，不对用户上传的真实截图提供识别结论。

### 自动化隔离回归

准备 Rust、JDK 21、Android SDK、OpenSSL 和 Python；USB 手机已配对且允许调试与通知。安装 Python 依赖后，从工作区运行：

```powershell
python -m pip install -r scripts/requirements-integration.txt
$env:JAVA_HOME = 'C:/Program Files/Android/Android Studio/jbr'
./scripts/verify-mock-flows.ps1 -DeviceSerial a1ae5637
```

脚本先检查三端契约快照，再重新构建三个消费者，避免旧中继拦截新契约回执。联调用本地服务显式启用 `integration-fixtures` 特性并保存到 `data/app-integration/fixture-bin`；随后重新生成普通二进制，供正常部署使用。只有测试构建识别 `tickdesk-integration-v1` 标记并允许在非交易时段跑成功扫描。其余身份、纪元、版本、事务、幂等、恢复和权限判断继续生效。

测试拥有临时证书、角色令牌、本地 SQLite 和独立 Android Room 数据库。通过 ADB 暂时转发 USB 端口，结束后恢复原转发及原 App。现有配对摘要必须前后一致。正常服务不加载测试供应商；不向现有台账写入样例持仓。

## 验收链路

| 链路 | 闭环断言 |
|---|---|
| 配对后的分页同步 | 23 个事项跨页完成，集合摘要一致，App 等完整快照后才可写 |
| 设置与版本冲突 | 保存后 App 投影与服务状态一致，旧版本更新被拒绝 |
| 事项稍后和忽略 | 动作版本推进、旧表单冲突、操作历史可查、终态通知不可重新打开 |
| 提醒 | 本地调度 → 持久 outbox → TLS 中继 → App 回查 → 系统通知；相同提醒只展示一次 |
| 断线重连 | 离线意图持久保存，重连后沿用原幂等身份执行一次 |
| 行情和关注 | 搜索 → 加关注 → 快照更新 → 日 K 翻页 → 移除关注 |
| 失败扫描 | 失败轮次可见，候选读取保持本轮，不能回退旧成功结果 |
| 对话 | 命令受理 → 最终答复 → 完整历史一致；假回答保留未核验标记 |
| 原图传输 | 分块上传 → 列表 → 下载 → 字节摘要一致 → 手动删除墓碑 |
| 截图导入 | 同意外发 → 识别草稿 → 人工编辑 → 差异预览 → 确认持仓；重复确认不重复入账 |
| 取消和撤销 | 取消后迟到结果不复活；撤销成功回执为 null，之后注册识别被拒绝 |
| 成交业务闭环 | 假行情扫描 → 新事项 → App 回填实际成交 → 成交记录 → 人工对账 → 持仓与估值同步 |
| 故障恢复 | 丢回执重发、中继/本地重启、备份恢复、新纪元隔离、证据封存、旧非成交操作归档 |

假数据中的激进档案返回成功候选，保守档案返回关闭日历的失败轮次；这只是可重复测试场景。测试证券价格、识别结果和文本答复不代表实际行情或 AI 判断。

## 本轮发现和修复

- 视觉授权撤销成功回执的 `consentedAt` 允许 null，并统一三端契约和构建产物。
- 本地 HTTP 内核探测显式初始化 TLS crypto provider，避免新增依赖配置导致启动 panic。
- 识别供应商已返回结果但本地提交暂时失败时，保留结果重试落库，不重复调用供应商。
- 暂停期间的定时扫描不再占用扫描租约，恢复扫描可以正常取得锁。
- Android 联调使用与实际 Runtime 相同的 IO 调度器，并显式启动可见 Activity；此手机在无可见页面时曾停止推进测试，打开 App 后立即恢复。真正退后台的能力由独立前台服务用例验收。设备单独验证 20 事项页面连续校验 10 次为 173 ms。
- Windows 诊断数据库连接显式关闭，失败路径也保存脱敏日志并恢复手机连接。
- 成功扫描的回归断言显式传入 `scanId`，避免定时扫描恰好生成新一轮时把“最新轮次”误当作刚提交的手动轮次。

## 验证记录与范围

结果记录在 `data/app-integration/logs/completion-*.log`。成功标准是日志中完整的 `PASS_ALL`、`OK (1 test)` 和服务端最终持久记录断言，不能仅凭页面能打开或 HTTP 200 判定通过。

2026-11-15 最后一次完整 USB 业务回归：`completion-android-final.log` 为 `OK (1 test)`，40.93 秒；阶段日志到达 `PASS_ALL`，测试本地库核对为 1 条持仓、2 条成交。原 USB 转发与已安装 App 配对摘要前后一致，结束后重新打开持久演示 App。服务间 9 项及 WS 提醒 3 项的结果分别在 `completion-services.log`、`completion-ws-reminders.log`。

当前量化内核全库检查有 62 个失败（4648 通过、15 跳过、1 xfailed），属于尚未完成的量化验收范围，本轮未将其宣称通过。直接接入相关的内核适配/工作进程/认证用例 20 个通过。真实行情规则、准入策略、硬风险事实与真实文本/视觉供应商效果不在假数据业务闭环结论内。

本目录其他设计文档的长期功能目标仍保留；此记录只对上述业务链路和实际执行结果负责。
