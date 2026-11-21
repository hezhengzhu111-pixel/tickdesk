# TickDesk 本地 Python AI

Python 后端提供对话、手动综合分析、仓位识别、可选外网采集和定时分析。Rust 持有身份、台账、任务、原图租约和最终提交。Android 继续使用 v2 对话、历史和图片导入页面。

| Android 输入 | 本地处理 | 可见结果 |
|---|---|---|
| 发送普通问题 | Python 读取冻结上下文与最近六轮对话 | 异步答复、单条引用及历史 |
| 发送 `/分析 600000.SH` | Python 生成八维报告，校验输出结构与证据，Rust 复核业务和量化输入 | 在现有对话页打开报告并追问 |
| 发送 `/报告 600000.SH` | 读取已保存的同纪元定时报告 | 返回缓存及其时间。过期或上下文改变时显示为历史报告 |
| 打开报告列表与详情 | 从当前配对和纪元的本地归档读取，分页与详情均无需调用模型 | 查看结论、时间、有效期、覆盖和来源 |
| 发送报告追问 | 按原报告或按当前情况选取证据。旧客户端的“现在”“当前”等表述仍可用 | 历史与当前结果分别标明时点 |
| 上传图片并授权识别 | Rust 一次性核验租约，Python 抄录，Rust 执行持仓校验并保存业务结果 | 直接导入须整批校验，结果在持仓原页展示。旧客户端也可使用已有人工确认命令 |

在手动分析文本内用 `[short_term]` 或 `[long_term]` 请求相应策略组。默认 `daily_swing`。缺少该组量化记录时会显示不可用。事实正文以固定预算传入，模型收到网页与历史对话数据但没有工具权限。保留已接受的 0 分和原始准入状态，持仓可卖信息来自现有规则投影，现金缺乏可核验依据时保持空值。

模型可按结论选择有来源的 `metricRefs`。本地渲染标的、字段、原值、单位及报告期，自由文本中的未核验数值会使答复失败。近期价量来自已有日线终态证据，财务比例按量化点时读取规则选取，报告保存所用的观察时间、来源、报告期和修订。资料缺失时显示覆盖不足。

## 本机启动

以 [assistant.example.toml](../config/assistant.example.toml) 为例修改自己的 Rust TOML，保留实际 `data_dir`、配对及量化设置。`[assistant]` 需要数字 loopback 地址和至少 43 字符随机令牌文件。使用受限目录和仅运行账号可读的 ACL，Python 与 Rust 使用同一令牌文件，券商、模型及 DeepSeek 凭据各自独立。

在 Tick Stock Panel 的 `backend` 目录配置并启动已安装依赖的 Python 后端。路径和模型需替换成实际值。

```powershell
$env:TICKDESK_AI_TOKEN_FILE = 'E:/private/tickdesk/python-assistant.token'
$env:TICKDESK_AI_LOCAL_ADDRESS = '127.0.0.1:18765'
$env:TICKDESK_AI_CHAT_ENDPOINT = 'https://model.example.com/v1/chat/completions'
$env:TICKDESK_AI_CHAT_MODEL = 'your-text-model'
$env:TICKDESK_AI_CHAT_KEY_FILE = 'E:/private/tickdesk/text.key'
$env:TICKDESK_AI_VISION_ENDPOINT = 'https://model.example.com/v1/chat/completions'
$env:TICKDESK_AI_VISION_MODEL = 'your-vision-model'
$env:TICKDESK_AI_VISION_KEY_FILE = 'E:/private/tickdesk/vision.key'

& .venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 3018 --workers 1 --no-proxy-headers
```

文本和视觉供应商使用 OpenAI-compatible 非流式响应。外部供应商须使用 HTTPS。本机假供应商允许数字 loopback HTTP。配置或凭据不完整时只返回脱敏错误，Rust 台账和量化继续运行。`[chat]`、`[vision]` 是旧直连配置，和 `[assistant]` 同时出现会导致显式配置错误。Python 离线时不会向旧通道补发同一任务。

同时使用量化时，必须启动上述完整 `app.main:app`，并在 Rust 配置 `[kernel] address = '127.0.0.1:3018'`。只挂 AI router 的精简主机没有行情仓库和策略引擎。量化探针分别检查健康、数据能力和生产策略目录；没有生产策略时保留扫描门禁，App 显示“已连接，尚无通过准入的生产策略”，不会自动提升研究策略。

扶摇同花顺 MCP 使用独立的 `TICKDESK_MCP_KEY_FILE` 文件配置，固定连接 `https://fuyao.aicubes.cn/mcp/a-share`。当前 Python 只读适配器开放交易日历和按指定证券查询快照；每五分钟执行握手、工具列表和实际日历工具调用，Rust 每十五秒读取认证诊断并同步给 App。失败返回脱敏原因，超过十一分钟的旧检查失效。此状态只证明 MCP 工具通道可用，不代表所有行情均新鲜、财报采集完整或已注入 AI 分析证据；不会触发模型调用、自动交易或推送操作建议。

仅启用自动分析时设置 `TICKDESK_AI_AUTO_ENABLED=1`。Python 每五秒轮询一个范围内的证券，按输入指纹决定初次、实质变化、风险事实更新或有效期届满是否排队。定时报告使用日线波段组，普通队列每分钟最多一项，北京时间每日 24 项中保留 4 项给风险更新。风险队列每 15 秒最多一项。响应未知的任务保持原结果状态。App 按需任务先于排队的定时分析，风险更新先于普通定时分析。

外网采集另设 `TICKDESK_FACTS_ENABLED=1`、`TICKDESK_FACTS_KEY_FILE`、`TICKDESK_FACTS_DAILY_USD`、`TICKDESK_FACTS_SEARCH_USD_PER_1000` 和 `TICKDESK_FACTS_FETCH_USD_PER_1000`。先按自己账号的实际价格填入统一 USD 币种预算。采集程序不内置供应商费率。采集库位于 Python `DATA_DIR/tickdesk-ai/facts.sqlite3`，保存正文、修订和费用预留，随 Python 数据目录单独备份。Rust 数据目录继续用现有备份流程。

文本和视觉使用各自的 `TICKDESK_AI_CHAT_BUDGET_`、`TICKDESK_AI_VISION_BUDGET_` 配置前缀。每组填写 `CURRENCY`（`CNY` 或 `USD`）、`DAILY`、`INPUT_PER_MILLION`、`OUTPUT_PER_MILLION`、`MAX_INPUT_TOKENS`、`MAX_CALLS`。价格及单次输入上界需从实际账号、模型和协议确认。费用预留使用配置的未缓存最高费率与单次输入输出上界。供应商返回 usage 后按实际 token 数重算保守成本，超时、无 usage 或中断保留预留。预算按北京时间自然日统计。已有无预算手动配置仍可发起用户主动请求并记录用量，外部自动分析必须具备已配置预算。

官方 DeepSeek `deepseek-flash` 和 `deepseek-v4-pro` 文本请求配置预算后，会从当日余额中预留单次输入成本，再选择余额可支付的最长输出。分配输出上限和写入预留在同一事务完成。剩余额度不足以覆盖供应商默认输出时，账本在发出请求前返回预算不足。可扩展的长回复使用该任务剩余时间。无预算的手动对话仍使用供应商默认输出。 [DeepSeek 输出参数](https://api-docs.deepseek.com/api/create-chat-completion/)

模型调用账本为 `DATA_DIR/tickdesk-ai/model-budget.sqlite3`，应与事实数据库分别做 SQLite 在线备份。`/api/tickdesk/ai/v1/status` 提供 `modelBudget` 配置和当日使用情况，包括调用次数、正文传输字节数、输入输出 token、未知计费状态及保守成本。显示的成本可按账号账单进一步核对。DeepSeek 对话、视觉、Search 和 Fetch 的实际费率及高峰时段应查供应商文档。[DeepSeek 模型和工具价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)

## 采集覆盖

| 类别 | 北京时间计划 |
|---|---|
| 单证券公告、风险、监管、停复牌、撤回与更正 | 全天每五分钟 |
| 行业和普通新闻、资金及市场舆情 | 交易时段按小时 |
| 宏观与政策 | 交易日 08:45、12:00、16:30、21:30 |
| 财报、经营、估值和预期 | 交易日 16:30 |
| 事件日历和外部规则资料 | 每日 16:30 |
| 主体资料 | 每周 16:30 |
| 休市重要事项 | 休市日 21:30 |

采集最多读取 64 个持仓、事项或关注证券，市场与行业查询共享。计划记录 due、attempt、success、覆盖窗口和下次到期。额度不足、交易日历缺失、访问失败、正文缺失或搜索无匹配各有状态。行情相关类别在日历未知时使用工作日估计并标记。公司材料需证券代码匹配，行业目前为市场共享范围。Fetch 对官方披露域名和路径有白名单。正文中的指令仅当作文本，无法核验的抽取数字不得用作交易规则。

## 本机接口和恢复

| 方向与版本 | 请求 | 结果 |
|---|---|---|
| Rust → Python `POST /api/tickdesk/ai/v1/chat` | v1、jobId、requestId、deadline、text、冻结 evidence，384 KiB | v1、原 jobId、model、result，256 KiB |
| Rust → Python `POST /api/tickdesk/ai/v1/vision` | v1、jobId、fileId、epoch、deadline、类型、base64 图片，28 MiB | 有界识别行。原图上限仍为 20 MiB |
| Python → Rust `POST /internal/ai/v1/vision/{jobId}/dispatch` | 同一 Bearer，epoch 与 fileId 请求头 | Rust 单次许可。取消、撤回、超时、纪元变化及重放返回冲突 |
| Python → Rust `GET /internal/ai/v1/scope` | Bearer | v1、纪元、有界证券范围和截断标记 |
| Python → Rust `GET/POST /internal/ai/v1/analysis/{symbol}` | 读一致快照。排队带有效截至时点、纪元、快照摘要、实质指纹和触发原因 | v1 排队或去重状态。输入过期返回冲突 |
| Python → Rust `GET /internal/ai/v1/report/{symbol}` | Bearer | 当前纪元定时报告与状态 |
| 运维 → Python `GET /api/tickdesk/ai/v1/status` | Bearer | 有限配置、任务运行、最近结果和事实服务状态 |

对话和识别桥接的有效返回会先在 Rust 本地暂存，再提交最终业务状态。App 请求的终态与 outbox 同事务保存。临时提交失败只重试提交。进程重启时，暂存结果按当前纪元与版本复核。网络已调用但无返回记录时标为 `CHAT_INTERRUPTED` 或 `VISION_INTERRUPTED`，供应商费用状态可能未知。换机和备份恢复后旧任务不能发布，旧记录可留存审计。

常见错误包括 `AI_NOT_CONFIGURED`、`AI_UNAVAILABLE`、`AI_TIMEOUT`、`AI_RESPONSE_TOO_LARGE`、`AI_INVALID_RESPONSE`、`AI_UNVERIFIED_SOURCE`、`AI_STALE_CONTEXT`、`AI_REPLAY` 和 `VISION_CONSENT_REQUIRED`。服务健康检查与 Python 的真实供应商请求结果独立。

回滚时停止隔离的 Python AI 调度并移除 Rust 的 `[assistant]`，可按原本的配置恢复旧直连模式。已有历史、台账、原图及量化观察沿用。旧模型逻辑暂时保留，便于逐步部署。源文件有 SHA-256 快照，按修改清单选择性恢复。

## 验证与现阶段限制

运行 `scripts/verify-local-ai.py` 验证真实 WSS、持久聊天、手动报告、定时报告去重与读取、原图授权、OCR 和导入。脚本启动隔离服务和假模型。Android 联调用 `scripts/integration_android.py --complete-functions --python-assistant`，显式指定设备；无配对的模拟器可以添加 `--allow-unpaired`。

定时报告可通过 `list_reports` 和 `get_report` 查阅。应用订阅后，新定时报告的发布事件由本地 outbox 投递。风险提醒仅对有引用证据的风险变化产生短时有效期，是否展示以 App 的状态核验为准。候选池及分行业覆盖、真实财经数据语义验证、账号金额配额仍需实际条件验证。报告可供查阅及追问，操作以台账、事项、预算和规则门禁为准。

2026-11-16 补充：USB 实体手机业务闭环、手机真实 DeepSeek 对话/报告和真实视觉适配器已完成本轮验证；覆盖与限制见父工作区 [手机联调记录](../../docs/testing/2026-11-16-phone-integration.md)。上述其余产品缺口仍保留，外网自动采集尚未启用。
