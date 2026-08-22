# Outcome

对齐前端接口审查中剩余的认证、版本操作和错误响应细节，消除实现与既定契约之间的低风险但可见偏差。

# Scope

- 校正认证 Cookie 的 `Secure` 属性配置，使部署环境的安全策略明确且可测试。
- 评估并对齐文档删除、投稿撤回的 `expected_version` 传参契约。
- 将恢复源已清理时的 410/409 响应对齐到既定版本恢复规范。
- 确认单文件上传的 415/422 错误码和错误体契约。
- 补齐旧版本读取租约的 `lease_token` 条件续租。
- 修正 transactional outbox 的零接收者处理、identity suppression 时机和物化前标题脱敏。

# Non-goals

- 不新增上传安全扫描族；由 `fix-admin-audit-security` 负责。
- 不修改文档生命周期主状态机、配额逻辑或聊天查询逻辑。
- 不改变已有前端可继续工作的兼容参数，除非正式契约明确要求对齐。

# Acceptance examples

- 认证 Cookie 的 Secure 行为在目标部署环境下符合明确配置，测试环境行为可被测试覆盖。
- 文档删除和投稿撤回的 `expected_version` 位置与前后端 contract 一致，冲突仍返回可消费错误。
- 恢复源被清理时返回设计约定的 409 及错误详情。
- 单文件上传的媒体类型/内容校验错误分别返回约定的 415/422 envelope。
- lease_token 不匹配时不能续租他人的读取租约；outbox 在 suppression、零接收者和标题脱敏场景下符合通知契约。

# Constraints and invariants

- 不降低生产环境 Cookie 或上传安全级别。
- 版本操作必须保留乐观锁和幂等语义。
- outbox 发布、delivery 创建和 suppression 的事务顺序必须保持可重放。
- 脱敏标题不得暴露被撤销身份的原始领域标题。

# Decisions

- 对已有前后端自洽但与文字规范不同的参数，先以现有公开 contract 和测试证据确认，避免无必要的破坏性迁移。
- 只修复审查中已确认或可通过 contract 验证的偏差。

# Build findings (2026-08-22)

- A2：后端 DELETE 传 query `expected_version`、withdraw 传 body 与前端 `settings/api.ts` 及 mock contract 测试一致，无需改动。
- A5 outbox：零接收者事件按保留期压缩（test_ops_and_compaction）、suppression ack 404（test_spec_audit_gaps）、物化前标题脱敏（redaction-then-materialization）均已有实现与测试覆盖，判定为已对齐，不改动。
- A1：新增 `RAG_AUTH_COOKIE_SECURE` 显式配置（缺省按 profile 推导，production=True）。
- A3：恢复源已清理由 410 改为 409 `document_version_purged`（含 document_id/document_version_id/purge_after_at_utc 详情）；内容读取路径保持 410 不变。
- A4：单文件上传（replace version）新增同步媒体校验：不支持类型 415 `unsupported_media_type`、声明类型与扩展名不符 422 `upload_content_type_mismatch`，与前端 upload contract 一致。
- A5 lease：`renew_lease` 改为条件续租，过期租约不能被复活。

# Open questions

无。Shape 阶段只记录目标与验收，确认后再进入实现。

# Verification expectations

- 运行 auth、documents、submissions、lease、outbox 和 notification contract 测试。
- 增加 Cookie、expected_version、410/409、415/422、lease_token 和 suppression/redaction 场景测试。
- 验证已有前端调用和重放/重试路径不回归。
