# Provider 出网统一规格

## 1. 唯一出网点

- `platform.provider` 是唯一出网点：embedding、contextual retrieval、图片 VLM、prompt-enhance、评测判官、chat 生成全部经统一 transport 内核出网；能力层不得直接构造模型 HTTP 请求（契约测试守卫）。
- 各能力点保留自身 adapter 与配置项；对外错误码与行为语义不变。

## 2. 韧性契约（§2.9 全量）

- 调用继承绝对 deadline；首次出站与每次短重试各分配唯一 `provider_call_id`。
- 重试策略：429/502/503/504/网络错误可重试，确定性 4xx 不重试；同步 ≤3 次、异步入库 ≤5 次；退避 250ms/1s/4s/16s+抖动、单次 ≤30s；HTTP 客户端关闭隐式自动重试。
- 熔断按 provider+operation 隔离：连续 5 次可重试失败 open 60s、half-open 单探测；判官与图片 VLM/生成 lane 互不借用容量；状态进指标与结构化日志。
- 熔断拒绝与 deadline 前置拒绝不产生 usage 事件。

## 3. 生产装配

- 生成 provider：`DASHSCOPE_API_KEY`（或全局 provider 配置）就绪时 runtime 装配 DashScope chat adapter（ChatProviderPort 生产实现，含流式语义）；未配置时保持 UnavailableChatProviderPort（dev/test 503 fail-closed）。
- ProviderReconciliationPort：生产实现提供 provider 侧结果确认查询（completed/not_sent/unknown 判定），供 usage unknown 对账与 chat 恢复流调用。
- usage 集成：`run_provider_call_with_usage` 是生产 provider 调用唯一包装（prepared→dispatching→completed/not_sent/unknown 生命周期）；embedding 自记 usage 逻辑并入，不存在平行记账。
