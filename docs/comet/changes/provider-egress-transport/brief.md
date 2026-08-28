# Outcome

provider 出网统一收口 `platform.provider`（用户已定方向）：五个 httpx 直连点全部经统一 transport 内核（短重试/退避/熔断）出网；生产生成 provider transport 接上真实 DashScope adapter（替换 UnavailableChatProviderPort 默认）；ProviderReconciliationPort 获得生产实现；usage 包装层成为唯一生产集成路径。对应审计 A3。

# Scope

- **统一收口**：embedding（`indexing/embedding.py`）、contextual retrieval（`contextual_provider.py`）、图片 VLM（`image_vlm.py`）、prompt-enhance（`chat/prompt_enhance.py`）、评测判官（`evaluation/judge.py`）五个直连点的 HTTP 构造与发送统一经 `platform.provider` 内核（`call_with_policy` + 共享 transport 构造）；各点配置项与对外错误码保持不变；judge 保留 lane 私有熔断隔离（按 provider+operation 键隔离，内核天然支持）。
- **生产生成 provider**：实现 DashScope chat adapter（`ChatProviderPort` 生产实现：生成/流式语义按 chat ports 契约），runtime 在 `DASHSCOPE_API_KEY`（或既有全局 provider 配置）就绪时装配；dev/test 保持 Unavailable 默认。
- **对账端口生产化**：`ProviderReconciliationPort` 生产实现（provider 侧结果确认查询），供 usage unknown 对账与 chat 恢复流消费（`worker-scheduling-wiring`、`chat-sse-contracts` 依赖此项）。
- **usage 集成为唯一路径**：`run_provider_call_with_usage` 成为生产 provider 调用的统一包装（embedding 现有自记 usage 逻辑并入，消除平行记账）；每次物理发送产生唯一 `provider_call_id`，熔断拒绝/deadline 前置拒绝不产生 usage 事件。

## Source coverage

| 来源单元 | 读取状态 | 保留语义 | Spec 位置 | 验收 ID | 覆盖状态 | 理由 |
| --- | --- | --- | --- | --- | --- | --- |
| 审计 A3（01-2.6、06-2.9、05-2.5.3-5）生产 transport 未接+直连绕过 | complete | 唯一出网点+生产 chat adapter+对账端口 | specs/provider-egress/spec.md §1–§3 | A1–A5 | covered | 设计 L35"platform.provider 是唯一出网点，能力层不得直接构造模型 HTTP 请求"+§2.9 韧性契约；用户已选"统一收口"方向（含接生产生成 provider） |

# Non-goals

- 不改变各能力点的对外错误码、行为语义与配置项名称。
- 不合并 judge 的独立预算/熔断 lane 设计（L1282；仅共享内核机制，隔离维度保留）。
- 不引入统一 provider 配置段重构（五套 settings 段保留，只统一 transport 执行路径）。

# Acceptance examples

- A1: 能力层（indexing/chat/evaluation/agents）不再出现直接构造模型 HTTP 请求的 `httpx.` 调用（唯一白名单：platform 层 transport 构造），以契约测试/静态检查断言。
- A2: 每个收口点在超时/429/503 场景下呈现 §2.9 行为：同步 ≤3 次、异步入库 ≤5 次、退避 250ms/1s/4s/16s+抖动单次 ≤30s、熔断连续 5 次 open 60s half-open 单探测（假 transport 断言调用序列）。
- A3: 每次物理发送产生新 `provider_call_id`；熔断拒绝/deadline 前置拒绝不产生 usage 事件。
- A4: 配置 DASHSCOPE_API_KEY 后 runtime 装配 DashScope chat adapter；ask→生成→SSE 全链经生产 transport（假 transport 契约测试 + 真实配置手动 smoke）；未配置时 dev/test 行为不变（Unavailable 503）。
- A5: ProviderReconciliationPort 生产实现可确认 completed/not_sent/unknown（假 transport 契约测试）；现有各域测试全绿（embedding_failed、contextual unavailable 等行为码稳定）。

# Constraints and invariants

- §2.9 韧性契约全量适用：绝对 deadline 继承、HTTP 客户端关闭隐式自动重试、429/502/503/504/网络错误可重试、确定性 4xx 不重试。
- provider port 只传递调用结果与关联标识，不拥有用量账本或 outbox 持久化（既有边界）。
- httpx client 生命周期统一管理（连接复用与 close 责任明确；prompt_enhance dispose 语义保留）。
- 不为收口添加新的配置面（复用各点既有配置）。

# Decisions

- 统一形态：各 adapter 保留、HTTP 执行统一走内核（`call_with_policy` + 共享 transport 构造），而非重写各域 adapter。
- embedding 的自记 usage 并入 `run_provider_call_with_usage` 包装（消除平行记账），`mark_unknown` 语义对齐 §2.9。
- chat adapter 归属 platform 层（platform.provider 域），chat 域只消费 port。
- DashScope chat adapter 的流式/非流式细节按 chat ports 契约与既有 prompt-enhance DashScope 实现风格实现（Build 阶段定稿）。

# Open questions

- [blocking] CONFIRM: 目标=五点收口+生产 chat adapter+对账端口生产化+usage 唯一包装；关键决策=adapter 保留/执行统一、embedding 记账并入、judge 保留 lane 隔离。请确认后本 change 才进入 Build。

# Verification expectations

- tests/platform/test_provider_policy.py 及各域契约测试（假 transport 驱动的重试/熔断/调用序列断言）全绿。
- 真实配置（DASHSCOPE_API_KEY）手动 smoke：ask 生成可用；未配置环境全量 pytest 不回归。
