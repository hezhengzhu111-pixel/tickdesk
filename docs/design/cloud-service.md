# 云端服务设计 v2

日期：2026-11-15。状态：待实现设计；本仓库导入的 im-foundation 不是本设计的可运行实现。协议及数值边界以 [三端接口契约](../11-三端接口契约.md) 和 [v2 契约包](../../contracts/README.md) 为准。

## 1. 目标与取舍

云端只运行一个 Rust WebSocket 中继进程 `relay`，提供最简身份验证、连接管理和有界内存转发。单用户、一台本地服务、一个 Android 安装实例。文件及历史保存在本地服务；App 保存自己的缓存和待发队列。云端不使用 MySQL、Redis、消息数据库、对象存储或云端离线信箱。

选择独立 `relay/` 工程，比从 im-foundation 的账号、租户、好友、附件和数据库模型中逐项裁剪更容易审计。保留导入源码作实现参考，不作为运行依赖，也不宣称其已有接口适配 v2。禁止用内存队列冒充可靠离线存储。业务端点离线时中继只报告 presence，发送方保留数据并等待。

| 保留 | 删除 | 由端点负责 |
|---|---|---|
| TLS、角色令牌、WS、心跳、限流、指标、健康检查 | 注册/密码/验证码/JWT 刷新、租户/RBAC、好友/群聊、管理后台、聊天/附件 REST、云端数据库 | 持久 outbox/inbox、业务幂等、历史分页、快照、文件存储、推送、AI、备份恢复 |

这使云重启不丢已被端点持久化的数据，但不能使离线的本地服务继续运行业务。两端存储及备份同时丢失时无法凭空恢复；本地断电时只显示缓存和待发送状态。

## 2. 部署与最简认证

公网只开放 `8443/TLS`，容器以非 root 用户运行、只读根文件系统；读取证书及只读 secrets 配置。云端持久内容仅为运维配置、证书和令牌摘要，**没有业务数据库和文件内容**。禁止把 WS 正文、Authorization、二维码、图片或持仓写日志。`/health/live` 只返回存活；`/health/ready` 检查配置有效、监听器和路由任务可工作。指标仅 loopback/运维内网可读。

首次配对由本地管理命令离线生成：pairId、分别属于 local/app 的 32 字节 CSPRNG 随机令牌、TLS SPKI SHA-256 pin。令牌编码固定为无 padding 的 base64url（43 个 ASCII 字符），摘要固定 SHA-256(ASCII(token))、小写 hex；不是对原始 32 字节求 hash。配置禁止同一摘要映射多个 pair/role。以挂载配置向云端部署令牌 SHA-256 摘要；本地保管 local 令牌。二维码只含 relayUrl、pairId、appToken、pins、protocol=2，不含 local 令牌或 AI 密钥。该二维码是凭据，扫码后不留相册、不展示完整日志。原型可手工导入同样结构，不创建云端注册流程。

WS 握手固定 `GET /relay/v2/connect`，HTTP `Authorization: Bearer <token>`，子协议 `tickdesk.v2`。令牌不放 URL/Cookie；云端恒定时间比较摘要，从命中配置确定 pairId 和 role，客户端不能自行选择对端或冒充 local。未知令牌返回 401；子协议错误 426；容量/限流 429（Retry-After）。App 凭据用 Android Keystore 保护，本地用仅服务账号可读的 secret；不得硬编码在 APK。

TLS 必须检查证书有效期、IP SAN 和受信 pin；首次 pin 从上述可信配对渠道取得，不能点击“忽略证书错误”。更换证书时先通过现有受信连接/本地重新配对配置下一 pin，过渡期双 pin；旧链已不可用则本地重新扫码。不通过不受信网络下载新 pin。客户端和本地服务都执行 pin 验证。

令牌丢失时：管理员替换该角色摘要、原子重载配置、立即关闭该角色旧连接（4401），重新配对；正常轮换可同时保留新旧摘要最多 10 分钟，随后撤销旧摘要并断开对应连接。配置错误拒绝整个重载且保留上份有效配置。摘要不是可用 Bearer。此方案没有刷新令牌和密码找回；设备丢失的恢复入口是本地管理命令。

## 3. 连接状态与会话隔离

```text
DISCONNECTED → TLS/AUTH → WAIT_HELLO → READY → PEER_OFFLINE / PEER_ONLINE
       ↑             失败/关闭/45 秒无有效 pong → BACKOFF
```

握手后 5 秒内客户端发送 hello，携带持久 endpoint incarnation；local 另携带当前 serverEpoch，app 携带最后已同步的 epoch（首次为 null）。incarnation 在普通进程重启、断网和云重启时不变；重装、清空数据库、恢复备份时必须新建。serverEpoch 则表示本地业务恢复纪元，正常启动不改变。两者不要混用。

云端校验 hello 后，为该角色分配 connectionId，在同一临界区替换活动连接并 fence 旧连接；然后返回 welcome 和双方 presence。旧连接之后的每一条数据、receipt、关闭回调都必须核对 connectionId，不能转发或误删新连接。旧连接收到 4409 后停止自动争抢，展示“连接被另一实例替换”；用户显式重试可重新接入。进程崩溃导致 welcome 丢失时新客户端正常重连即可。

welcome 回显 pairId（端点必须与配对配置一致），并含 relayBootId、connectionId、heartbeatIntervalMs=15000、heartbeatTimeoutMs=45000；presence 是云端生成的完整 peer 状态（online/incarnation/serverEpoch），只发给同 pair 的另一角色。客户端不能发送 presence/welcome。路由目标由认证 pair+相反角色推导，不接受客户端提供任意目标 ID。

双方 ready 并交换 presence 后才能发送 deliver。peer incarnation 改变时端点先隔离旧队列，走第 6 节恢复，不能把待发旧意图悄悄改绑新 incarnation。云的 relayBootId 改变只要求刷新 presence 和重发未确认项，不代表业务纪元变化。

## 4. 心跳、超时与重连

连接两端每 15 秒发送 WebSocket Ping（8 字节随机 nonce）；收到 Ping 立即回同 payload 的 Pong。保留最近 3 个待确认 nonce，以单调时钟计时，45 秒未收到匹配 Pong 关闭连接；普通业务流量不能伪装有效 Pong。一个方向仍通不代表双向正常。控制帧优先处理，文件传输不得阻塞心跳。RFC 定义 Ping/Pong 及异常断线后的退避建议；具体 15/45 秒是本项目待实测默认值。[RFC 6455](https://www.rfc-editor.org/rfc/rfc6455.html)

每个端点仅有一个连接管理器，统一处理 Wi-Fi/移动网络切换、超时和进程生命周期，避免多层重试相乘。建立连接总超时 10 秒；初次失败等待 uniform(0,1s)，第 n 次失败等待 uniform(0,min(30s,1s×2^n))，连续健康 60 秒才清零计数。网络恢复可提前唤醒一次并加 0–1 秒随机延迟。429 遵守 Retry-After（最多 300 秒）；401/4401、证书错误、协议不兼容停止重试并显示可操作原因。1006/1011/1012/1013 可重试；4409 需用户处理。超时不能推导业务未执行；重试保留幂等键。[AWS 重试与退避](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/)

Android Doze、权限及厂商后台策略下不能保证常驻 WS。App 前台先连接/同步，后台断连靠 MiPush 最小事件引用提示重新拉取；推送不替代业务事实或持久队列。即使推送不可达，本地仍保存事件，App 下次恢复可查询。强提醒的系统权限降级见 App 设计。

## 5. 三种确认与端到端可靠投递

本方案不设置“云接收即成功”回执。socket send 成功仅说明进入网络栈。

| 信号 | 发送者 | 精确定义 | 允许界面显示 |
|---|---|---|---|
| send 返回 | 客户端网络库 | 数据尝试发往中继 | 发送中 |
| receipt | 对端 | 完整消息/hash/schema校验且 inbox/文件块持久化 | 对端已保存、等待处理 |
| 业务 ack | 本地服务 | 业务事务/拒绝与幂等记录、响应 outbox 一并提交 | 已生效/明确失败/需对账 |

TCP 和 socket 写入不能证明应用已经保存或处理；消费确认与发送确认也不是一回事。这里借鉴确认语义，不引入 RabbitMQ 依赖。[RabbitMQ 可靠性说明](https://www.rabbitmq.com/docs/reliability)

发送端先事务保存不可变 deliveryId、payloadHash、payload、fromIncarnation、toIncarnation，再尝试发送。收到匹配四元组（deliveryId、hash、发送 incarnation、接收 incarnation）的 receipt 才标记 delivered。接收端以发送角色+incarnation+deliveryId 去重：同 hash 重发 receipt，不重复副作用；不同 hash 报 DELIVERY_ID_REUSED，隔离数据。同一业务命令重试可换 deliveryId，但不能换 clientMsgId 或 data；优先复用原 deliveryId。

receipt 的 fromIncarnation/toIncarnation **原样回显原 deliver**，不因返回方向而交换。云端校验 receipt 当前发送连接 incarnation==toIncarnation、目标活动连接 incarnation==fromIncarnation；deliver 则反向核对 sender==from、peer==to。端点不能伪造另一连接的 receipt。

接收端先验证大小、UTF-8、JSON 重复键、schema、hash及目标 incarnation，再持久 inbox，才发送 receipt。业务 worker 从 inbox 消费；命令的状态变更、clientMsgId+requestHash 去重结果、业务 ack、后续事件 outbox 在一个 SQLite 事务提交。worker 崩溃可重入；receipt 丢失可重复投递。SQLite 采用 WAL+synchronous=FULL，磁盘错误不发送 receipt；这依赖存储设备正确实现刷新。[SQLite synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous)

收到 receipt 仍未收到业务 ack 的 App 命令保持待确认，15 秒后查询/以原 clientMsgId 重发，退避到 30 秒，后台暂停至可执行时恢复；不能显示失败后自动创建新命令。业务 ack 也使用相反方向 deliver/receipt。首次业务拒绝也保存，改变意图必须新 clientMsgId。异步 reply/import_result 由对应持久任务继续产生，不因 accepted ack 清除任务。所有可写业务字段校验及 serverEpoch 校验在本地执行。

未 receipt 的传输记录不因 TTL 静默删除。已 receipt 的业务事件原始记录至少保留 30 天，业务账本、命令幂等结果及成交记录长期保留。App 未取得业务 ack 的命令原文长期保留直至明确对账/人工归档。临时文件块可在最终文件确认后回收，源文件留存策略见第 7 节。磁盘配额不足时拒绝新输入并显式告警，不丢最旧未确认项来“自愈”。

## 6. 重连与恢复

普通重连：重新验证身份 → hello/welcome/presence → 原 incarnation 对未 receipt 项重发 → App 请求业务 sync → 未 ack 命令用原幂等键重查/重发。事件按 eventId 去重、对象 version 合并，不能用最大 snapshotSeq 丢弃较小但不同对象事件。没有云端历史接口，也没有累计传输游标，因此不会把乱序消息的最大序号当作完整接收进度。

本地备份恢复：启动前更换 local incarnation 和 serverEpoch，关闭业务写入/扫描/提醒；App 隔离旧 epoch 队列和回执，按 recovery_evidence → seal → reconcile → confirm 上传证据并人工确认。旧命令不能自动重放到新 epoch。普通云重启不触发该流程。

App 重装/数据库恢复：新 app incarnation，先 sync 全量权威业务状态及 get_messages 历史；本地旧定向 outbox 标记等待新端同步，不自动将旧提醒重放为新提醒。完成快照后用户显式处理仍需核对的意图。App 旧 incarnation 中的未确认命令恢复后置 quarantined；即使 serverEpoch 未变，也调用只读 get_command_status(originalEpoch,targetClientMsgId,targetRequestHash) 查询：found 返回 ackEventId 并重投原 ack；processing 等待；quarantined 保持隔离；not_found 只表示未知，不能自动重发执行；hash_mismatch 禁止关联。只有用户完成核对后可创建新的明确意图。两端有任一 incarnation 变化都不能直接沿用旧 receipt。本地收到新 app incarnation 时，在业务事务串行边界更新当前 peer 并隔离旧 incarnation 的未执行业务 inbox。worker 提交前在同一事务再次核对 peer incarnation/epoch；切换先提交则旧 worker 不再执行，业务先提交则状态查询返回原 ack。get_command_status 必须同时检查命令账本和 inbox，不能把尚在处理或已隔离的项报 not_found。

事件超出 30 天保留期用业务快照/本地历史恢复；临时传输日志不承担永久审计。无完整证据的真实成交进入 needs_reconciliation，不推断“没有发生”。恢复操作必须包含文件 manifest，缺原图标记 missing，可继续查看已确认识别记录，但不能伪装可重识别。

## 7. 本地文件与流式中转

文件命令属于本地服务业务 API：begin_upload / get_upload / finalize_upload / list_files / read_file / delete_file / cancel_transfer；识别另有 register_image / cancel_import。云端只按普通有界帧转发。仅 PNG/JPEG，单文件上限 20 MiB，解码像素上限 40 MP；32 KiB 原始块，Base64 编码，单传输窗口 4 块，最多 2 个并行文件任务。文件名仅显示用途，不能用于拼接路径；服务端生成 fileId。

App 选择图片后复制到应用私有待发目录、计算 SHA-256，保存上传意图。begin_upload 返回 uploadId、fileId、总字节数/总块数；get_upload 返回已持久块号，重连据此补传。每个 uploadId/fileId 使用串行 actor（及数据库状态 CAS）处理块写、finalize、过期和删除；并发相同块不能各自通过“尚未存在”检查。单块 hash 校验后写临时文件并刷新，再提交块 manifest 才发送 receipt；相同块号不同 hash 拒绝。最后一块长度精确等于剩余字节，累计尺寸必须与声明一致。

finalize_upload 校验所有块、总 SHA-256、真实 MIME/解码尺寸，刷新完整文件并原子 rename，刷新目录，再提交 metadata=available 和幂等 ack。崩溃后启动扫描“文件已 rename 但 metadata 未提交”的候选，只能重新验 hash 后完成原操作或隔离，不能提前回应成功。上传临时碎片 24 小时不活动后可清理，并返回 UPLOAD_EXPIRED；**这不适用于已经保存的原图**。cancel_transfer成功或新get_upload明确expired/cancelled后，两端将相关块记为终态并停止重发；不可仅凭本机TTL或超时丢弃未确认消息。

本地原图长期保留，用户手动 delete_file 后删除原图与缩略图，保留不含图片内容的 fileId/hash/删除时间墓碑及识别/成交审计。识别进行中有持久 lease，删除返回 FILE_BUSY，用户以 cancel_import 取消识别并释放 lease 后重新发起删除（新 clientMsgId；已保存的 FILE_BUSY 拒绝用原键重试仍返回该拒绝）；不在正在读取时删除。识别/下载 lease 获取与 available 检查、删除状态 CAS 使用同一事务；识别持久 deadline 默认 120 秒，取消/超时/结束均释放，迟到 AI 结果不得复活 cancelled 导入。删除先持久 deleting 状态，禁止新识别/读取，再删 bytes 并提交 deleted；中途崩溃继续删除，原 clientMsgId 重试返回同一结果。备份可能仍含历史原图，删除界面提示备份过期窗口，恢复时重放最新可用删除清单；无法取得新于备份的删除清单时，恢复原图先隔离待用户对账，不能假装知道备份后发生的删除。默认备份保留14天；不承诺第三方 AI 已删除。

read_file 返回短期 downloadId/块数/hash，并将文件块作为 file_chunk 从 local 发给 app；App 持久验证块并 receipt，完成后核验总 hash。下载全部块收到匹配 receipt 后释放 lease；cancel_transfer(kind=download) 释放并停止后续发送，上传取消则清理临时块，均持久取消墓碑防止迟到块重建。断线不续租；本地作为下载权威在24小时到期时持久化expired、将该downloadId未receipt块标记expired并停止重发，然后释放lease。App依据已确认read_file.expiresAt显示下载需重试，重新read_file创建新会话；不得因此把原图判为已删。客户端崩溃不会永久阻塞删除。取消完成前已交付的缓存由 App 清理，不能远程抹除相册。App 查看缓存离开后可清理；上传私有副本仅在 finalize 成功且本地可用状态已确认后清理；不删除用户相册原件。

## 8. 有界资源与可观测

WS 仅文本 JSON，不启用压缩；一条完整 WS 消息上限 128 KiB（包括分帧累计），业务 envelope ≤64 KiB，file_chunk 原始块 ≤32 KiB。禁止一边无限接收碎片一边等待 FIN；每条消息 10 秒组装截止。每 pair 最多 2 个活动连接；新握手总数/速率另限流。默认单进程最多 100 pair（本项目配置 1）。每连接待写队列最多 32 条且总计 1 MiB；达到任一上限返回 BUSY（refDeliveryId 可用）或关闭 1013，发送端保留原记录重试。

调度：控制帧先行；receipt/error/presence 独立保留 16 KiB 控制预算；剩余业务与文件按 8:1 加权轮询，每次最多一个文件块，不允许文件饿死也不允许文件洪峰占用所有业务位置。业务 in-flight 上限 16、文件 in-flight 4；超时后原记录退避重试，不叠加新拷贝。接收端 inbox 接近容量停止接收并报告 DISK_FULL，不确认未落盘消息。

核心指标：各角色连接状态、心跳 RTT、last pong age、重连原因、队列条数/字节数、receipt 延迟、业务 ack 延迟（由端点报告匿名聚合）、重复数、hash 冲突、丢弃非法帧、磁盘容量。日志只含随机 correlationId/错误码/耗时，不含业务内容。health ready 不是“本地在线”，App 分别显示网络、云连接、本地在线、同步时间。

## 9. 实现拆分与验收

`relay/` 模块：config/auth → ws acceptor → session registry/fence → bounded router → heartbeat → metrics。云端开发使用模拟端点即可完成；不需要运行量化或 Android。

| 注入点 | 必须观察到的结果 |
|---|---|
| 云在转发前/后重启 | sender outbox 仍在，重连后最终到达；业务副作用一次 |
| 收端落盘后 receipt 丢失 | 重发被去重，重复 receipt；无重复成交/任务 |
| 业务提交后 ack 丢失 | 同 clientMsgId 返回原结果，App 不误建新意图 |
| 旧 socket 延迟数据/关闭回调 | 被 fence 拒绝，不转发、不移除新 socket |
| 单向黑洞/代理空闲断开 | 活跃进程在 45 秒心跳期限加调度误差内检测并退避 |
| 文件上传中网络切换/两端重启 | 已持久块不重写、不误报完成，补传后总 hash 相同 |
| 磁盘满/文件 rename 前后崩溃 | 无伪 receipt/伪 finalize 成功；可恢复或明确失败 |
| 本地/ App 恢复旧备份 | incarnation fence 生效；旧意图隔离，对账后才可写 |
| 文件洪峰并发命令 | 心跳正常、业务无饥饿、内存不超界 |
| 凭据撤销/错误 pin | 连接终止/停止自动重试，不降级明文 |

先做确定性故障注入与 10,000 次随机断连/重复/乱序模型演练，再做实际 Rust/Android/磁盘故障联调、24 小时文件与消息并发 soak。模型和 schema 通过不能替代这些实现验收。SLO 在真实设备/网络基准后确认；本设计不承诺零故障、无限离线时长或 Android 后台实时可达。
