# TickDesk 三端契约包

当前版本 **2.0.0**。配套 [接口规范](../docs/11-三端接口契约.md) 与 [云端设计](../docs/design/cloud-service.md)。所有数据为模拟样例，所有服务接口均为待实现设计。

| 文件 | 用途 |
|---|---|
| [business.schema.json](v2/business.schema.json) / [commands.json](v2/commands.json) | 49 个业务命令、严格请求/结果/事件 DTO |
| [relay.schema.json](v2/relay.schema.json) / [relay-fixtures.json](v2/relay-fixtures.json) | 连接、presence、deliver、receipt、error、文件块 |
| [cloud.openapi.json](v2/cloud.openapi.json) | 2 个健康检查 + 1 个 WS 升级入口；无云文件/历史 API |
| [pairing.schema.json](v2/pairing.schema.json) | 首次二维码配置，不能作为公开样例存放真实凭据 |
| [fixtures.json](v2/fixtures.json) / [errors.json](v2/errors.json) | 独立业务场景和错误目录，valid=false 为故意非法数据 |
| [hash-vectors.json](v2/hash-vectors.json) / [evidence-vectors.json](v2/evidence-vectors.json) | 跨语言规范化、恢复证据字节与摘要 |
| [push.example.json](v2/push.example.json) | 最小事件引用；不能直接当强提醒事实 |

本地与 App 共用全部业务/传输样例；云端只实现认证、路由和传输层，不执行业务命令。角色方向、事务、文件大小、跨字段规则也必须遵守接口规范，不能只靠 schema。

```powershell
python -m pip install -r contracts/requirements.txt
python contracts/validate.py
python contracts/check_docs.py
python contracts/check_ui.py
```

`validate.py` 默认检查 v2；`--legacy` 只检查历史 v1。`validate_v2.py` 包含有限状态参考模型，不能证明真实服务的并发与持久化。`build_v2.py` 从冻结 v1 DTO 基线可复现生成 v2；修改应更新生成器并重新生成/验证，避免下次覆盖手改。当前不提供产品 Mock 服务器，各端可将 fixtures 注入 Repository/协议适配器。

`v1/`、源码锁及旧 IM REST/2000 字符分片仅为历史证据，不是 v2 运行依赖。

文档/原型检查另需 Node.js，仅运行语法与最小DOM桩，不启动浏览器，不验证视觉布局。
