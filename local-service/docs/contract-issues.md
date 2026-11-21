# v2 契约问题

## 已修复：视觉告知撤销的成功回执

位置：工作区 docs/11-三端接口契约.md §3 和 §7，contracts/v2/business.schema.json 的 cmd_accept_vision_notice、result_accept_vision_notice 和 VisionNotice；fixtures.json 有 request_revoke_vision_notice。

冲突：accepted=false 必须撤销告知并将 consentedAt 清空为 null。VisionNotice 允许 null，但 result_accept_vision_notice 要求 consentedAt 为非负整数。使用撤销时间或 0 会把未授权伪装为授权。

状态：2026-11-15 已在共享生成器将结果 consentedAt 改为可空，新增独立撤销请求和成功回执样例并同步三端。接受/撤销测试通过。三端必须一起重新编译，旧中继二进制会拒绝新撤销回执。

建议：共享构建器把结果中的 consentedAt 改为可空时间，增加接受及撤销双向成功 ack 样例，三端同时更新。已经发送给视觉供应商的数据不属于可撤销的本地状态。

## 恢复证据首片缺少会话 ID 的 schema 约束

位置：docs/11-三端接口契约.md §7 和 business.schema.json 的 cmd_recovery_evidence。

冲突：恢复流程按 reconciliationId 隔离，当前 schema 在 properties 中声明该字段，却没有列入 required。在当前只有一个活动会话的本地实现中，缺省绑定当前恢复会话，显式给定时必须与当前会话匹配。

影响：若未来允许并行恢复会话，省略该字段会产生歧义。若三端当前预期必须显式指定，也会导致相同报文判定分歧。

建议：在共享生成器明确必填或单活动会话的缺省规则，并补对应样例。
