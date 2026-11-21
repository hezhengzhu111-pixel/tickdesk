# 本机双服务与 USB 手机联调

首次部署于 2026-11-15。云端中继、本地服务和 Python 助手均运行在这台 Windows 电脑，使用独立、持久化的 `data/app-integration` 目录。手机通过 ADB reverse 访问中继。此部署为开发进程，未注册为开机自启服务。

## 连接配置

| 项目 | 值 |
|---|---|
| USB 手机 | `a1ae5637` |
| App WebSocket | `wss://127.0.0.1:8443/relay/v2/connect` |
| WebSocket 子协议 | `tickdesk.v2` |
| pairId | `usb_24ba1e1bcbd48db7` |
| 公钥 pin | `sha256/bj4l7FvFuC4Z+zPkG10Uq24zzjqLWlxZ0KqPzwEwKnE=` |
| 中继健康检查 | `https://127.0.0.1:8443/health/ready` |
| 本地管理接口，仅电脑访问 | `http://127.0.0.1:18765` |
| 中继指标，仅电脑访问 | `http://127.0.0.1:18444/metrics` |
| 服务器证书有效期 | 至 2026-12-15 11:12:06，中国标准时间 |

`127.0.0.1` 在手机上依赖 USB 转发；拔线后不可连接。App 只连接中继，不直接访问本地管理接口。

配对文件为 `data/app-integration/app-pairing.json`，包含 App Bearer 令牌，已复制至手机 `Download/tickdesk-usb_24ba1e1bcbd48db7.json`。不要提交或公开分享该文件。运行目录已限制为当前 Windows 用户和 SYSTEM 可访问；令牌、私钥、数据库、日志不放入源码。

## App 接入

2026-11-16 21:17 已在原手机同签名覆盖安装 `data/app-integration/tickdesk-usb-debug.apk`，SHA-256 为 `9A2DA789FA6F9798582C2035280BBC2D0A8561D3201B0F3F62293D20825C033A`。配对文件和提醒偏好摘要保持，原配对已同步并收到 `get_reply_page` 能力。旧版同签名包保存在 `data/app-integration/tickdesk-usb-20261116-195442.apk`。覆盖统计见「设置 → 工作台服务状态」。 [三端工件清单](../../.superpowers/sdd/2026-11-16-business-closure/release/full-reply-and-ledger/candidate-manifest.json) · [本轮验收记录](2026-11-16-business-closure.md)

如需在另一台未配对的开发设备上手动接入：

1. 安装上述 USB 调试包后，进入 App 的设置页，选择导入配置文件。
2. 在手机 Download 中选择 `tickdesk-usb_24ba1e1bcbd48db7.json`。
3. 核对 URL、pairId 和上方完整 pin，确认并保存连接。
4. 首次同步的台账、事项为空是正常状态；部署没有写入示例持仓或成交。

本轮采用 WS 提醒。进入 **设置 → 后台提醒**，允许通知后开启开关，状态栏会显示连接状态并提供关闭入口。新安装时开关默认为关闭。2026-11-16 18:09 读取原手机确认系统拒绝通知，当前配对尚未开启后台提醒。开启后离开 App 仍可保持连接。强制停止、系统省电限制或 USB 断开会中断实时提醒，重新打开 App 后可恢复。

USB 调试构建仅针对 `127.0.0.1` 信任本次开发 CA，仍执行证书链、主机名和 OkHttp pin 校验。普通 debug 和 release 不引入此 CA。信任配置使用 Android 的 [Network Security Configuration](https://developer.android.com/privacy-and-security/security-config)。

重建 USB 调试包（项目根目录执行）：

```powershell
$env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk"
Push-Location app
.\gradlew.bat :core:test :mobile:assembleDebug :mobile:lintDebug '-PtickdeskUsbIntegration=true' --project-cache-dir ..\data\app-integration\android-gradle-cache --no-daemon
Pop-Location
# 上一条构建成功后复制产物
Copy-Item app/mobile/build/usb-integration/outputs/apk/debug/mobile-debug.apk data/app-integration/tickdesk-usb-debug.apk
```

设置该属性后，Gradle 各模块输出位于 `build/usb-integration`，与普通 App 构建分开。CA 资源为本机生成文件；如果资源缺失，使用下文 Prepare 恢复，原有令牌和数据会保留。

## 日常启动、停止和日志

在仓库根执行：

```powershell
# 启动两项服务并恢复手机端口转发；已启动的进程不会重复创建
.\scripts\app-integration.ps1 -Action Start

# 查看后台进程
.\scripts\app-integration.ps1 -Action Status

# 仅恢复 USB 转发（重插 USB 后可用）
.\scripts\app-integration.ps1 -Action Usb

# 停止本部署的进程；保留数据、凭据和转发配置
.\scripts\app-integration.ps1 -Action Stop
```

脚本以可执行文件路径、PID 和启动时间共同确认进程归属；端口被其他程序占用时不会终止它。运行的可执行文件位于 `data/app-integration/bin`，不会锁住两端工程的 `target/debug` 构建产物。日志位于 `data/app-integration/logs`。

更新服务代码后，先正常构建，再 Stop、Start，让启动脚本复制新产物。重启 Windows 后需要重新 Start；手机必须已连接并授权 USB 调试。

```powershell
cargo build --manifest-path relay/Cargo.toml --locked
cargo build --manifest-path local-service/Cargo.toml --locked
```

### 更新、核验和回退

1. 在根目录运行 `python contracts/validate.py` 和 `python scripts/sync_contracts.py --check`，构建两项 Rust 服务和 App。保留当前部署的两个 EXE 和上一版已签名 APK 到私有运行目录中的新子目录，以便回退。
2. 执行 `./scripts/app-integration.ps1 -Action Stop`。用下节方式备份两个业务数据目录。运行 `./scripts/app-integration.ps1 -Action Start` 同时启用 USB，或使用 `-SkipUsb` 在手机离线时只部署电脑服务。
3. 执行 `./scripts/app-integration.ps1 -Action Verify -Apk <新APK>`。它通过已校验证书链、主机名和公钥 pin 的 TLS 读取中继，核对运行中两个 Rust 服务、APK 和本次契约快照，输出构建和文件摘要。本地依赖就绪仍看 `/health/ready`。
4. 在指定手机上用 `adb -s <设备序列号> install -r <新APK>` 更新同签名应用。保持原配对，按需执行 `-Action Usb -DeviceSerial <设备序列号>`。打开工作台，确认业务状态及本轮新增能力可以读取。
5. 同契约且兼容当前数据的 Rust 回退可从私有旧产物目录运行 `-Action Start -RustArtifacts <旧产物目录>`。先运行 Stop。若涉及持久化版本变化，先在隔离恢复目录验证旧版本读写，再决定切换。不要将备份覆盖回当前数据目录。App 仅使用同签名且可读取当前本地数据的构建。

### 封存和切换 Python 助手

在根目录生成新的发布目录，记录输出的 SHA-256。封存 Python 源码、启动器和运行所需的公开资源。解释器与依赖使用现有后端虚拟环境，`DATA_DIR` 及原有私有配置继续使用现有位置。

```powershell
$runtime = (Resolve-Path data/app-integration).Path
$package = Join-Path $runtime ('releases/python-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path (Split-Path -Parent $package) -Force | Out-Null
$digest = & .\tick-stock-panel\backend\.venv\Scripts\python.exe scripts/assistant_release.py seal --source tick-stock-panel --output $package
if ($LASTEXITCODE -ne 0) { throw 'Python 助手封存失败' }
```

有既有量化研究配置时，可在切换前用下列命令预热新产物的量化编译缓存。它只读当前行情与研究配置，输出实际完成的策略评估数，不调用模型或供应商，也不生成扫描、报告或影子档案。缓存写入数据目录的 `.runtime-cache/numba`，封存包仍按原清单严格校验。

```powershell
$env:DATA_DIR = (Resolve-Path data/app-integration/python-data).Path
& .\tick-stock-panel\backend\.venv\Scripts\python.exe -I -B "$package/assistant_host.py" --sha256 $digest --warm-quant-cache
```

备份现有 `config/assistant-release.json` 后，将新产物的绝对路径和 SHA-256 写入该文件，字段为 `path` 和 `sha256`。执行 `./scripts/app-integration.ps1 -Action RestartAssistant` 只切换助手。脚本在停止旧助手前核对新产物摘要，启动后通过本机认证状态核对当前摘要。执行上节的 `Verify` 用已安装 `requirements-integration.txt` 依赖的 Python 核对三端产物。回退时恢复上一份发布配置再执行 `RestartAssistant`。首次从开发模式切换时，移走这次新增的发布配置可恢复原启动方式。

### 离线整套备份

下列路径按现有 USB 部署填写。私有运行根目录的访问权限由 Prepare 建立。备份和演练目标各使用未创建的目录名。

```powershell
$runtime = (Resolve-Path data/app-integration).Path
$archive = Join-Path $runtime 'before-workbench-upgrade'
$restored = Join-Path $runtime 'isolated-restore-check'
.\scripts\app-integration.ps1 -Action Stop
python scripts/workbench_backup.py --binary "$runtime/bin/tickdesk-local.exe" backup --config "$runtime/config/local.toml" --python-data "$runtime/python-data" --destination $archive
python scripts/workbench_backup.py --binary "$runtime/bin/tickdesk-local.exe" restore --archive $archive --destination $restored
.\scripts\app-integration.ps1 -Action Start -SkipUsb
```

备份前确认此目录的 Rust 与 Python 写入进程已停止。若私有配置使用其它 `DATA_DIR`，将命令的 Python 数据参数指向实际目录。归档包含 Rust 数据库、原图、Python 事实、量化和历史源数据及核对摘要。报告与冻结上下文随 Rust 台账保存。供应商密钥、配对凭据与运行日志沿用原私有配置。自动日备份仍覆盖 Rust 数据库和原图，跨两套存储的备份用上面的停机方式。

Python 归档保存业务证据数据库的已提交内容，运行跟单扩展的进程互斥文件 `user_data/extensions/paper_execution/runner.sqlite` 由下次启动创建。业务 SQLite 连续 5 秒没有备份进展时，脚本报告对应文件并清理暂存。锁释放后可用原目标路径重试。完成的 Python 数据库以独立文件归档。

恢复将 Rust 数据和 Python 证据一并准备到新目录。启动隔离演练时，为 Rust 使用独立监听和配对，为 Python 指定恢复后的独立 `DATA_DIR`。核对报告证据、原图恢复隔离区和历史台账，然后通过恢复门禁处理差异。

核验已封存的 SQLite 备份时，旧归档使用 `mode=ro&immutable=1`，避免只读连接生成归档清单之外的 SQLite 辅助文件。备份工具修订版把副本改为 `DELETE` 日志模式后再封存，正常运行台账继续使用 WAL。恢复仍严格核对清单及每个文件的摘要。 [实档复验](2026-11-16-business-closure.md#1103-真实归档恢复和只读审计)

直接查询归档里的 SQLite 时使用 `mode=ro&immutable=1` 打开 URI，或先查询临时副本。普通只读 WAL 连接仍可能在同目录创建辅助文件，归档完整性检查会拒绝多余文件。

本机首次准备使用如下命令；Python 需要 `cryptography`，当前环境已安装。Prepare 在已有部署时不轮换凭据、不重置数据库：

```powershell
.\scripts\app-integration.ps1 -Action Prepare -DeviceSerial a1ae5637
```

服务器证书到期后需重新签发并重启中继；不要通过删除运行目录重新初始化来续期，因为其中包含持久数据。

## 持续观察与供应商预算

从项目根目录启动只读采样。输出使用新的文件名，每分钟记录本地和中继健康、发布版本、原配对同步及待发数量。封存的助手每次核对启动报告和目录文件摘要，结果存为 `assistantRelease`。电脑停止运行会中断采样。

```powershell
python scripts/observe-workbench.py --runtime data/app-integration --output data/app-integration/workbench-observation.jsonl --minutes 1440
```

供应商预算由账户持有人写入 `data/app-integration/config/assistant-env.json`。`TICKDESK_AI_CHAT_BUDGET_` 和 `TICKDESK_AI_VISION_BUDGET_` 各使用 `CURRENCY`、`DAILY`、`INPUT_PER_MILLION`、`OUTPUT_PER_MILLION`、`MAX_INPUT_TOKENS`、`MAX_CALLS` 六个后缀。每个模型独立填写币种、每日金额、每百万输入和输出 token 费率、单次输入上限和日调用上限。币种支持 `CNY` 和 `USD`。

Search 和 Fetch 共用 `TICKDESK_FACTS_` 配置前缀。填写 `CURRENCY`（`CNY` 或 `USD`）、`DAILY`（合计日限额）、`SEARCH_PER_1000`、`FETCH_PER_1000`（账户实际费率）；可加 `SEARCH_DAILY` 与 `FETCH_DAILY` 分别限制两类金额。`SEARCH_CALLS_PER_DAY`、`FETCH_CALLS_PER_DAY`、`SEARCH_CALLS_PER_MINUTE`、`FETCH_CALLS_PER_MINUTE` 可进一步缩小本地次数上限。旧配置继续使用 `DAILY_USD`、`SEARCH_USD_PER_1000` 和 `FETCH_USD_PER_1000`，计价仍为美元。切换币种需等上海时区的下一自然日。 [DeepSeek 网页搜索与抓取](https://api-docs.deepseek.com/zh-cn/guides/search/)

配置修改后，按上节步骤重启。可先保持 `TICKDESK_AI_AUTO_ENABLED` 与 `TICKDESK_FACTS_ENABLED` 关闭，读取 Python 状态检查模型预算配置。状态探针只访问本机，输出不含访问令牌。事实采集费用与权限在启用采集后另行验收。

```powershell
$cfg = Get-Content data/app-integration/config/assistant-env.json -Raw | ConvertFrom-Json
$auth = [IO.File]::ReadAllText($cfg.TICKDESK_AI_TOKEN_FILE).Trim()
$status = Invoke-RestMethod http://127.0.0.1:3018/api/tickdesk/ai/v1/status -Headers @{Authorization="Bearer $auth"}
$status.modelBudget.configuration
$status.factBudget
Remove-Variable auth
```

启用大盘与行业的盘中采集时，在同一私有配置中填写 `TICKDESK_REGIME_COLLECTION_ENABLED=1` 与单个 `TICKDESK_REGIME_COLLECTION_SYMBOL`，并保持 `TICKDESK_QUANT_DATA_ENABLED=1`。标的应属于现有工作台或研究范围。每个标的每交易日只领取一次任务，发请求前检查交易日历和行情时段。默认关闭该采集。深交所风险警示快照可通过 `TICKDESK_SZSE_ST_CAPTURE_ENABLED=0` 暂停。

`./scripts/app-integration.ps1 -Action VerifyAssistantConfig` 可以在运行中核对本机配置形状和封存产物，只输出开关状态。使用 `RestartAssistant` 时，会在停止原进程前校验配置。

`factBudget` 显示当前配置币种、合计与单项金额上限，以及当日 Search/Fetch 的尝试数、计费数、预留或已计费用。输入按查询词或 URL 的 UTF-8 字节数计量，返回按已收到的标题、摘要和正文计量，同时列出实际计量了多少次。旧记录未保存的字节数保持未知。配置探针不执行供应商请求。

## 验证与已知范围

2026-11-15 首次 USB 部署的历史验证包括以下记录。

- 中继 `/health/live`、`/health/ready` 均为 200；本地 `/health/live` 为 200。
- 中继指标确认 local 在线；协议探针完成 App 角色认证、hello/welcome/presence、`get_status` 的持久接收回执和业务 ACK，并校验 v2 schema。
- USB 手机上通过 curl 访问中继 ready，证书 CA、IP 主机名和公钥 pin 校验通过。未使用跳过证书校验选项。
- App 43 个 core 测试通过，USB APK 与测试 APK 构建成功；lint 0 错误、3 警告，其中旧 Lifecycle 自定义检查与当前 lint API 不兼容，另两项为 SDK 更新建议。最终构建日志位于 `data/app-integration/logs/three-tier-final-android-build.log`。
- 指定 USB 手机已实际保存加密配对配置并完成 WSS 首次同步；自动化配对测试 1 项通过。详细证据见 [WS 提醒验证报告](2026-11-15-ws-reminders.md)。
- 最终真机完整业务用例 1 项、现有工作台读取 1 项、后台服务 3 项均通过。验证分页同步、写入与冲突、系统提醒与去重、断线重连、文件传输，以及常驻通知、后台真实业务 ACK、关闭服务后前台连接保持、权限拒绝和失效启动不崩溃。测试后恢复原有后台提醒开启偏好，App 已重新打开，前台服务运行。详见 [三端联调报告](2026-11-15-three-tier-integration.md)。

2026-11-16 20:44，电脑端更新「今日复盘」和「持仓风险」的本地量化输入及正文显示。21:03 正常桌面运行全文分页及持仓证据保护版本，21:17 原手机同签名更新并重连。正常 DeepSeek 已送达两次带量化和来源的复盘，一份综合报告已送达并归档。本地 `/health/ready` 为 **503**，等待 `kernel` 和 `vision`。命令能力由 `get_status.protocol.supportedCommands` 提供。 [26 项和 20 个场景验收记录](2026-11-16-business-closure.md)

21:40 本地 Rust 更新使报告目录在手动分析完成后自动刷新，使用上述手机安装包。21:50 正常 Python 接入按时点筛选的行业及指数输入，该版本持续观测从 21:51:11 开始。 [发布及实际使用记录](2026-11-16-business-closure.md) · [21:50 版连续观测](../../.superpowers/sdd/2026-11-16-business-closure-facts-budget/release-20261116-215000-continuity-24h.jsonl)

22:11 正常 Python 已归档现有扶摇日历。`/api/tickdesk/ai/v1/status` 的 `regimeCollection` 可核对原观察时间、摘要与请求计数，新增外部采集尚未启用。 [实际状态](../../.superpowers/full-business-closure/regime-lifecycle-normal-readback.log) · [当前连续观测](../../.superpowers/sdd/2026-11-16-business-closure-facts-budget/release-20261116-221110-continuity-24h.jsonl)

复核手机链路（当前手机带 `/system/bin/curl`；curl 的 pin 前缀是 `sha256//`，与配对 JSON 的 `sha256/` 有区别）：

```powershell
$adb = "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe"
$meta = Get-Content data/app-integration/deployment.json -Raw | ConvertFrom-Json
$ca = "/data/local/tmp/tickdesk-$($meta.pairId)-ca.pem"
& $adb -s $meta.deviceSerial push data/app-integration/secrets/ca.pem $ca
& $adb -s $meta.deviceSerial shell curl --noproxy 127.0.0.1 --cacert $ca --pinnedpubkey $meta.pin.Replace('sha256/','sha256//') --connect-timeout 5 --max-time 10 --fail --silent --show-error https://127.0.0.1:8443/health/ready
```

协议探针只在首次部署且没有已登记的真实 App 时运行；真实 App 配对后不要用另一 incarnation 接管同一个 pair。此前完整的两进程故障恢复联调见 [检查与联调报告](2026-11-15-services-review-and-integration.md)。

## 2026-11-17 17:31 正常 Python 助手发布与模拟器隔离验收

正常桌面在 16:36 已切换到本地 Rust `rust-focused-analysis-20261117`（本地与中继工件 SHA-256 分别为 `61916f3f…`、`e808d122…`）。17:31 只切换正常 Python 助手，发布目录为 `data/app-integration/releases/python-analysis-evidence-bind-20261117`，SHA-256 为 `409b3f44…`；上一版封存配置备份在 `data/app-integration/config/assistant-release-before-r5-20261117.json`，回退时恢复该文件后执行 `-Action RestartAssistant`。切换前预热了量化缓存，切换后核对发布摘要、本机认证状态、三端契约快照、原配对文件、部署配置、助手环境、台账对象、历史对话与四张原图，前后指纹一致。真实 DeepSeek 单次调用已在隔离 TLS 与 Rust 上为该版本生成并重启读回报告。 [切换回执](../../.superpowers/full-business-closure/android-gestures-settings-20261117/assistant-r5-cutover.json) · [真实模型回执](../../.superpowers/full-business-closure/android-gestures-settings-20261117/python-analysis-evidence-bind-real-deepseek.json)

本阶段 Android 验收按用户要求使用模拟器、独立配对和隔离工作台，实体手机测试暂缓。隔离运行时位于 `data/app-integration/emulator-isolated-20261117`，拥有独立中继（`127.77.0.35:8443`）、本地 Rust、封存 Python 助手和 `emu_979b65bbfccb36f8` 配对；正常服务只读复制，不写入正常台账。运维脚本为 `.superpowers/full-business-closure/android-gestures-settings-20261117/isolated_workbench.py`（`prepare`/`start`/`stop`/`status`/`push`），启动后通过 ADB reverse 将设备 8443 转发到隔离桥接端口。2026-11-17 17:34 在该环境从「持仓 → 量化与 AI」经真实 DeepSeek 完成综合分析，报告 2,467 字符、20 条来源、4 条收盘研究原值，App 正文、报告详情、重启重开、来源弹窗与两类追问均与隔离台账一致。 [模拟器验收汇总](../../.superpowers/full-business-closure/android-gestures-settings-20261117/emulator-real-deepseek-acceptance.json) · [完整验收记录](2026-11-16-business-closure.md#1734-真实-deepseek-综合分析与模拟器隔离验收)

### 18:13 日线覆盖归因助手发布

正常 Python 助手进一步切换到 `data/app-integration/releases/python-coverage-attribution-20261117`，SHA-256 为 `801fc621…`；上一版（`409b3f44…`）配置备份在 `data/app-integration/config/assistant-release-before-attribution-20261117.json`，回退时恢复该文件后执行 `-Action RestartAssistant`。本版新增日线覆盖的官方停牌归因：助手启动循环对最近 10 个交易日增量补抓上交所/深交所停复牌应答，发布日线时把缺失单元归因写入 `tickdesk-market/history-coverage.json`，并在本机认证状态 `marketHistory.attribution` 中输出已归因/未解释数量。2026-11-17 的 198 个交易日、1,236 个缺失单元全部归因为官方整日停牌，未解释 0；v2 `historyCoverage` 公共结构与三端契约快照保持不变。 [切换回执](../../.superpowers/full-business-closure/coverage-attribution-20261117/attribution-cutover.json) · [产品证据](../../.superpowers/full-business-closure/coverage-attribution-20261117/attribution-product.json) · [归因验收记录](2026-11-16-business-closure.md#1807-日线覆盖官方停牌归因)

### 18:27 v2 覆盖归因三端发布

正常桌面已整体切到 v2 覆盖归因版本：Rust 发布目录 `data/app-integration/releases/rust-coverage-attribution-20261117`（local `62fcb6f8…`、relay `45a972fb…`），Python 发布目录 `data/app-integration/releases/python-coverage-attribution-v2-20261117`（`d95bd63a…`），Android USB 包 `data/app-integration/releases/android-coverage-attribution-20261117/tickdesk-usb-debug.apk`（`a7477a5d…`）。三端契约快照已同步，`-Action Verify` 通过（契约清单 `e2cd436a…`）。旧 Rust 工件备份在 `data/app-integration/normal-rust-before-coverage-20261117-1826`；旧助手配置备份在 `data/app-integration/config/assistant-release-before-coverage-v2-20261117.json`。回退时恢复该配置并 `-Action Stop` / `-Action Start -RustArtifacts <旧目录> -SkipUsb`。部署前后原配对、台账、对话和原图保留；watchlist 成员不变，仅服务刷新的 dataAsOf 变化。App 设置页「全市场历史覆盖」新增缺失归因、归因窗口和交易所停牌证据；模拟器已安装新包并与隔离工作台验证。实体手机已于 18:47 同签名覆盖安装覆盖归因包，19:12 更新为含中继重启恢复修复的 `android-relay-recovery-20261117`（`e3fb16f3…`，默认 USB 包已同步），重新导入原配对 `usb_24ba1e1bcbd48db7`，覆盖归因、真实 DeepSeek 报告与杀进程重开回读均通过；通知/锁屏、截图、成交对账、断网/重启等场景继续按表执行。 [发布证据](../../.superpowers/full-business-closure/coverage-attribution-20261117/coverage-three-end-deploy.json) · [模拟器截图](../../.superpowers/full-business-closure/coverage-attribution-20261117/emulator-coverage-attribution.png)

当前交付版本、运行环境与回退入口汇总见 [交付说明与最终状态](2026-11-17-final-delivery.md)。

### 19:54 聊天答复重放与重启恢复发布

正常桌面更新到聊天恢复版本：Python 助手 `releases/python-chat-outcome-replay-r3-20261117`（`e6249b58…`，新增 `tickdesk-ai/ai-outcomes.sqlite3` 答复重放缓存），本地 Rust `releases/rust-chat-outcome-replay-20261117`（local `702f4c2b…`，重启时重排未到期聊天并重取助手答复），Android `releases/android-local-restart-recovery-20261117`（`bda57bfd…`，修复排队帧被误判为终态协议错误）。`-Action Verify` 通过；实体手机已安装并验证任务中重启后自动重连、答复完整且模型只调用一次。回退时恢复对应旧发布目录与助手配置即可；旧本地工件仍保留在 `normal-rust-before-coverage-20261117-1826`，旧助手配置备份在 `config/assistant-release-before-chat-replay-20261117.json`。

### 设备测试注意

运行 `:mobile:connectedDebugAndroidTest` 前必须设置 `ANDROID_SERIAL=emulator-5566`（或断开实体手机），否则测试任务会对所有连接设备安装测试包并卸载正式包；实体手机被卸载后重新安装默认包并导入原配对文件即可恢复，服务端台账不受影响。
