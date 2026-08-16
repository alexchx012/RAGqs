/*
 * 管理面板契约 mock 测试（§6.12、§8.1–8.5、§9.1–9.2、§10.1–10.2、§11.1–11.3、§12.1–12.7）。
 * 全部经 HTTP 层（MSW）断言：状态码 / 错误 envelope code / 读模型字段 / 幂等回放与通知联动；
 * 夹具开关（graph 预估与终态、校准资格、部门停用未核实）直接调控制器方法。
 */

import { describe, expect, it } from 'vitest';
import type {
  AdminDepartmentItem,
  AdminDepartmentListResponse,
  AdminUserItem,
  AdminUserListResponse,
  CalibrationWindow,
  DashboardResponse,
  GraphBuildCancelResponse,
  GraphBuildCurrent,
  GraphBuildRun,
  LeaderboardResponse,
  OpsJobsResponse,
  PermissionMatrixResponse,
  QuotaApproveResponse,
  QuotaRejectResponse,
  QuotaRequestListResponse,
} from '../admin/types';
import { resolveUrl } from '../api/client';
import type {
  ApprovalDecisionResponse,
  ApprovalListResponse,
  ApprovalSummary,
} from '../settings/types';
import { mockAdmin, mockAuth, mockKnowledge } from './testing';

function bearerOf(username = 'zhangsan', password = 'password123'): string {
  const { accessToken } = mockAuth.login(username, password, 'vitest');
  return `Bearer ${accessToken}`;
}

interface RequestOptions {
  readonly method?: string;
  readonly body?: unknown;
  readonly idempotencyKey?: string;
}

async function jsonRequest(token: string, path: string, options: RequestOptions = {}): Promise<Response> {
  const headers: Record<string, string> = { Authorization: token };
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }
  if (options.idempotencyKey !== undefined) {
    headers['Idempotency-Key'] = options.idempotencyKey;
  }
  return fetch(resolveUrl(path), {
    method: options.method ?? 'GET',
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
}

interface ErrorEnvelope {
  readonly error: {
    readonly code: string;
    readonly details: Record<string, unknown>;
  };
}

async function expectError(response: Response, status: number, code: string): Promise<ErrorEnvelope> {
  expect(response.status).toBe(status);
  const body = (await response.json()) as ErrorEnvelope;
  expect(body.error.code).toBe(code);
  return body;
}

async function postWithKey(
  token: string,
  path: string,
  body: unknown,
  key: string,
): Promise<Response> {
  return jsonRequest(token, path, { method: 'POST', body, idempotencyKey: key });
}

/* ---------- §9.1 dashboard ---------- */

describe('admin contract：dashboard（§9.1）', () => {
  it('ops 组包：待办卡联动实时 pending 计数；api_latency 为无数据卡', async () => {
    const ops = bearerOf('ops-wang');
    const response = await jsonRequest(ops, '/v1/metrics/dashboard?window=7d');
    expect(response.status).toBe(200);
    const body = (await response.json()) as DashboardResponse;
    expect(body.window).toBe('7d');
    const cards = body.packs.flatMap((pack) => pack.cards);
    const quotaCard = cards.find((card) => card.key === 'quota_pending');
    const submissionCard = cards.find((card) => card.key === 'submission_pending');
    expect(quotaCard?.kind).toBe('count');
    expect(quotaCard && 'value' in quotaCard ? quotaCard.value : null).toBe(3);
    expect(submissionCard && 'value' in submissionCard ? submissionCard.value : null).toBe(4);
    const latency = cards.find((card) => card.key === 'api_latency');
    expect(latency && 'value' in latency ? latency.value : undefined).toBeNull();
    // 运维卡可携带阈值与跳转键
    expect(cards.some((card) => card.threshold !== null)).toBe(true);
    expect(cards.some((card) => card.link !== null)).toBe(true);
  });

  it('backlog 是当前快照，事件卡仍按窗口变化', async () => {
    const ops = bearerOf('ops-wang');
    const today = (await (await jsonRequest(ops, '/v1/metrics/dashboard?window=today')).json()) as DashboardResponse;
    const month = (await (await jsonRequest(ops, '/v1/metrics/dashboard?window=30d')).json()) as DashboardResponse;
    const cardValue = (dashboard: DashboardResponse, key: string) => {
      const card = dashboard.packs.flatMap((pack) => pack.cards).find((candidate) => candidate.key === key);
      return card && 'value' in card ? card.value : null;
    };
    const backlog = (dashboard: DashboardResponse) => {
      const card = dashboard
        .packs.flatMap((pack) => pack.cards)
        .find((candidate) => candidate.key === 'ingestion_backlog');
      return card?.kind === 'stat' ? card : null;
    };

    expect(cardValue(today, 'ingestion_backlog')).toBe(26);
    expect(cardValue(month, 'ingestion_backlog')).toBe(26);
    expect(backlog(today)?.sparkline).toEqual([4, 6, 5, 9, 12]);
    expect(backlog(month)?.sparkline).toEqual([4, 6, 5, 9, 12]);
    expect(cardValue(today, 'failure_rate')).not.toBe(cardValue(month, 'failure_rate'));
  });

  it('admin 组包：结构不同、threshold/link 恒 null、user_rank 15 行', async () => {
    const admin = bearerOf('admin');
    const response = await jsonRequest(admin, '/v1/metrics/dashboard?window=7d');
    expect(response.status).toBe(200);
    const body = (await response.json()) as DashboardResponse;
    expect(body.packs.map((pack) => pack.key)).not.toContain('todo');
    const cards = body.packs.flatMap((pack) => pack.cards);
    expect(cards.every((card) => card.threshold === null)).toBe(true);
    expect(cards.every((card) => card.link === null)).toBe(true);
    const rank = cards.find((card) => card.kind === 'user_rank');
    expect(rank && rank.kind === 'user_rank' ? rank.rows.length : 0).toBe(15);
  });

  it('其余角色 403；非法窗口 422', async () => {
    const user = bearerOf('zhangsan');
    await expectError(await jsonRequest(user, '/v1/metrics/dashboard?window=7d'), 403, 'metrics_forbidden');
    const ops = bearerOf('ops-wang');
    await expectError(await jsonRequest(ops, '/v1/metrics/dashboard?window=bogus'), 422, 'validation_error');
  });

  it('指标看板（§9.2）：固定三卡无组包，窗口沿用', async () => {
    const ops = bearerOf('ops-wang');
    const response = await jsonRequest(ops, '/v1/metrics/operations?window=today');
    expect(response.status).toBe(200);
    const body = (await response.json()) as { window: string; cards: unknown[] };
    expect(body.window).toBe('today');
    expect(body.cards.length).toBe(3);
  });
});

/* ---------- §8.1 审批计数（admin 域遮蔽 knowledge 同名路由） ---------- */

describe('admin contract：approvals summary（§8.1）', () => {
  it('ops：quota_pending 由 admin 域计数，submission_pending 为公共库范围', async () => {
    const ops = bearerOf('ops-wang');
    const body = (await (await jsonRequest(ops, '/v1/approvals/summary')).json()) as ApprovalSummary;
    expect(body).toEqual({ quota_pending: 3, submission_pending: 4 });
  });

  it('admin / 部长 / 普通用户：quota_pending 恒 0，投稿计数按角色范围', async () => {
    const admin = bearerOf('admin');
    expect(((await (await jsonRequest(admin, '/v1/approvals/summary')).json()) as ApprovalSummary)).toEqual({
      quota_pending: 0,
      submission_pending: 7,
    });
    const minister = bearerOf('minister-li');
    expect(((await (await jsonRequest(minister, '/v1/approvals/summary')).json()) as ApprovalSummary)).toEqual({
      quota_pending: 0,
      submission_pending: 2,
    });
    const user = bearerOf('zhangsan');
    expect(((await (await jsonRequest(user, '/v1/approvals/summary')).json()) as ApprovalSummary)).toEqual({
      quota_pending: 0,
      submission_pending: 0,
    });
  });
});

/* ---------- §8.2–8.3 配额申请审批 ---------- */

describe('admin contract：配额申请审批（§8.2–8.3）', () => {
  async function pendingQuotaIds(token: string): Promise<QuotaRequestListResponse> {
    const response = await jsonRequest(token, '/v1/approvals/quota-requests?status=pending');
    expect(response.status).toBe(200);
    return (await response.json()) as QuotaRequestListResponse;
  }

  it('待办列表按申请时间正序；status=approved 过滤出历史', async () => {
    const ops = bearerOf('ops-wang');
    const pending = await pendingQuotaIds(ops);
    expect(pending.items.map((item) => item.applicant.id)).toEqual(['u_user', 'u_minister', 'u_ghost']);
    const approved = await jsonRequest(ops, '/v1/approvals/quota-requests?status=approved');
    const approvedBody = (await approved.json()) as QuotaRequestListResponse;
    expect(approvedBody.items.length).toBe(1);
    expect(approvedBody.items[0]?.approved_pages).toBe(80);
    await expectError(
      await jsonRequest(ops, '/v1/approvals/quota-requests?status=bogus'),
      422,
      'validation_error',
    );
    const admin = bearerOf('admin');
    await expectError(
      await jsonRequest(admin, '/v1/approvals/quota-requests'),
      403,
      'approval_forbidden',
    );
  });

  it('批准：200 + 默认页数；幂等回放同快照；同键不同体 409；通知联动', async () => {
    const ops = bearerOf('ops-wang');
    const pending = await pendingQuotaIds(ops);
    const target = pending.items[0];
    expect(target?.applicant.id).toBe('u_user');

    const approved = await postWithKey(
      ops,
      `/v1/approvals/quota-requests/${target?.id}/approve`,
      { expected_version: 1 },
      'idem_q_1',
    );
    expect(approved.status).toBe(200);
    const approvedBody = (await approved.json()) as QuotaApproveResponse;
    expect(approvedBody).toMatchObject({
      id: target?.id,
      version: 2,
      status: 'approved',
      approved_pages: 100,
      quota_period: '2026-08',
    });
    expect(approvedBody.credit_entry_id).not.toBe('');

    // 幂等回放：同键同体返回首次快照（version 不再推进）
    const replay = await postWithKey(
      ops,
      `/v1/approvals/quota-requests/${target?.id}/approve`,
      { expected_version: 1 },
      'idem_q_1',
    );
    expect(replay.status).toBe(200);
    expect(((await replay.json()) as QuotaApproveResponse).version).toBe(2);

    // 同键不同体：409 idempotency_key_conflict
    await expectError(
      await postWithKey(
        ops,
        `/v1/approvals/quota-requests/${target?.id}/approve`,
        { expected_version: 1, approved_pages: 50 },
        'idem_q_1',
      ),
      409,
      'idempotency_key_conflict',
    );

    // 终态不可再审：409 already_processed
    await expectError(
      await postWithKey(
        ops,
        `/v1/approvals/quota-requests/${target?.id}/approve`,
        { expected_version: 2 },
        'idem_q_2',
      ),
      409,
      'already_processed',
    );

    // 通知联动：申请人收到 quota_approved
    const user = bearerOf('zhangsan');
    const notifications = (await (await jsonRequest(user, '/v1/notifications')).json()) as {
      items: { type: string }[];
    };
    expect(notifications.items.some((item) => item.type === 'quota_approved')).toBe(true);
  });

  it('批准错误系列：version_conflict / not_approvable / 非 ops 403 / 缺 key 422', async () => {
    const ops = bearerOf('ops-wang');
    const pending = await pendingQuotaIds(ops);
    const ministerRequest = pending.items.find((item) => item.applicant.id === 'u_minister');
    const ghostRequest = pending.items.find((item) => item.applicant.id === 'u_ghost');

    await expectError(
      await postWithKey(
        ops,
        `/v1/approvals/quota-requests/${ministerRequest?.id}/approve`,
        { expected_version: 9 },
        'idem_q_3',
      ),
      409,
      'version_conflict',
    );
    await expectError(
      await postWithKey(
        ops,
        `/v1/approvals/quota-requests/${ghostRequest?.id}/approve`,
        { expected_version: 1 },
        'idem_q_4',
      ),
      409,
      'quota_request_not_approvable',
    );

    const admin = bearerOf('admin');
    await expectError(
      await postWithKey(
        admin,
        `/v1/approvals/quota-requests/${ministerRequest?.id}/approve`,
        { expected_version: 1 },
        'idem_q_5',
      ),
      403,
      'approval_forbidden',
    );
    // 缺 Idempotency-Key：422
    await expectError(
      await jsonRequest(ops, `/v1/approvals/quota-requests/${ministerRequest?.id}/approve`, {
        method: 'POST',
        body: { expected_version: 1 },
      }),
      422,
      'validation_error',
    );
  });

  it('驳回：200 + 通知联动；请求体多字段 422', async () => {
    const ops = bearerOf('ops-wang');
    const pending = await pendingQuotaIds(ops);
    const target = pending.items.find((item) => item.applicant.id === 'u_minister');

    const rejected = await postWithKey(
      ops,
      `/v1/approvals/quota-requests/${target?.id}/reject`,
      { expected_version: 1 },
      'idem_q_6',
    );
    expect(rejected.status).toBe(200);
    expect(((await rejected.json()) as QuotaRejectResponse)).toEqual({
      id: target?.id,
      version: 2,
      status: 'rejected',
    });

    const minister = bearerOf('minister-li');
    const notifications = (await (await jsonRequest(minister, '/v1/notifications')).json()) as {
      items: { type: string }[];
    };
    expect(notifications.items.some((item) => item.type === 'quota_rejected')).toBe(true);

    const another = (await pendingQuotaIds(ops)).items.find((item) => item.applicant.id === 'u_ghost');
    await expectError(
      await postWithKey(
        ops,
        `/v1/approvals/quota-requests/${another?.id}/reject`,
        { expected_version: 1, reason: 'x' },
        'idem_q_7',
      ),
      422,
      'validation_error',
    );
  });
});

/* ---------- §8.4–8.5 投稿审核（复用 knowledge 端点） ---------- */

describe('admin contract：投稿审核（§8.4–8.5）', () => {
  async function listSubmissions(token: string): Promise<ApprovalListResponse> {
    const response = await jsonRequest(token, '/v1/approvals/submissions');
    expect(response.status).toBe(200);
    return (await response.json()) as ApprovalListResponse;
  }

  it('ops 只见公共库；admin 见公共库 + 全部部门库', async () => {
    const ops = bearerOf('ops-wang');
    const opsList = await listSubmissions(ops);
    expect(opsList.items.length).toBe(4);
    expect(Object.keys(opsList.items[0] ?? {}).sort()).toEqual([
      'created_at',
      'file_name',
      'media_kind',
      'reviewed_at',
      'space_id',
      'status',
      'submission_id',
      'version',
    ]);
    expect(opsList.items.every((item) => item.space_id === 'public')).toBe(true);

    const admin = bearerOf('admin');
    expect((await listSubmissions(admin)).items.length).toBe(7);
  });

  it('批准 202；重复审批 409 submission_already_reviewed', async () => {
    const ops = bearerOf('ops-wang');
    const target = (await listSubmissions(ops)).items.find((item) => item.file_name === '行业研报汇总.pdf');
    const approved = await postWithKey(
      ops,
      `/v1/approvals/submissions/${target?.submission_id}/approve`,
      { expected_version: 1 },
      'idem_s_1',
    );
    expect(approved.status).toBe(202);
    expect(((await approved.json()) as ApprovalDecisionResponse).status).toBe('approved');
    await expectError(
      await postWithKey(
        ops,
        `/v1/approvals/submissions/${target?.submission_id}/approve`,
        { expected_version: 2 },
        'idem_s_2',
      ),
      409,
      'submission_already_reviewed',
    );
  });

  it('duplicate_document 行不移除；scope_changed 行失效；冻结投稿人 409', async () => {
    const ops = bearerOf('ops-wang');
    const items = (await listSubmissions(ops)).items;
    const duplicate = items.find((item) => item.file_name === '公共制度汇编.pdf');
    await expectError(
      await postWithKey(
        ops,
        `/v1/approvals/submissions/${duplicate?.submission_id}/approve`,
        { expected_version: 1 },
        'idem_s_3',
      ),
      409,
      'duplicate_document',
    );
    expect(
      (await listSubmissions(ops)).items.some((item) => item.submission_id === duplicate?.submission_id),
    ).toBe(true);

    const scopeChanged = items.find((item) => item.file_name === '跨部门协作指引.pdf');
    await expectError(
      await postWithKey(
        ops,
        `/v1/approvals/submissions/${scopeChanged?.submission_id}/approve`,
        { expected_version: 1 },
        'idem_s_4',
      ),
      409,
      'submission_scope_changed',
    );
    expect(
      (await listSubmissions(ops)).items.some((item) => item.submission_id === scopeChanged?.submission_id),
    ).toBe(false);

    const frozen = items.find((item) => item.file_name === '历史遗留材料.pdf');
    await expectError(
      await postWithKey(
        ops,
        `/v1/approvals/submissions/${frozen?.submission_id}/approve`,
        { expected_version: 1 },
        'idem_s_5',
      ),
      409,
      'submitter_pending_delete',
    );

    const normal = (await listSubmissions(ops)).items.find((item) => item.file_name === '行业研报汇总.pdf');
    await expectError(
      await postWithKey(
        ops,
        `/v1/approvals/submissions/${normal?.submission_id}/approve`,
        { expected_version: 99 },
        'idem_s_6',
      ),
      409,
      'version_conflict',
    );
  });
});

/* ---------- §6.12 公共库图谱维护 ---------- */

describe('admin contract：图谱构建（§6.12）', () => {
  it('admin 访问 403；ops 读模型含 availability 与 source_revision', async () => {
    const admin = bearerOf('admin');
    await expectError(
      await jsonRequest(admin, '/v1/ops/graph-builds/current'),
      403,
      'graph_build_forbidden',
    );
    const ops = bearerOf('ops-wang');
    const current = (await (await jsonRequest(ops, '/v1/ops/graph-builds/current')).json()) as GraphBuildCurrent;
    expect(current).toMatchObject({
      space_id: 'public',
      source_revision: 12,
      graph_availability: 'stale',
      latest_run: null,
    });
    expect(current.active_generation?.graph_generation_id).toBe('gg_11');
  });

  it('202 创建 → 轮询推进至 succeeded → availability ready + 终态通知发起者', async () => {
    const ops = bearerOf('ops-wang');
    const created = await postWithKey(ops, '/v1/ops/graph-builds', { expected_source_revision: 12 }, 'idem_g_1');
    expect(created.status).toBe(202);
    const run = (await created.json()) as GraphBuildRun;
    expect(run).toMatchObject({
      state: 'queued',
      version: 1,
      source_revision: 12,
      estimated_primary_model_calls: 3,
      allowed_actions: ['cancel'],
    });

    // 构建进行中重复创建：409
    await expectError(
      await postWithKey(ops, '/v1/ops/graph-builds', { expected_source_revision: 12 }, 'idem_g_2'),
      409,
      'graph_build_in_progress',
    );

    const running = (await (await jsonRequest(ops, '/v1/ops/graph-builds/current')).json()) as GraphBuildCurrent;
    expect(running.latest_run).toMatchObject({ state: 'running', version: 2 });
    const succeeded = (await (await jsonRequest(ops, '/v1/ops/graph-builds/current')).json()) as GraphBuildCurrent;
    expect(succeeded.latest_run).toMatchObject({ state: 'succeeded', version: 3 });
    expect(succeeded.graph_availability).toBe('ready');
    expect(succeeded.active_generation?.source_revision).toBe(12);

    // 幂等回放：同键同体返回首个 run（不产生新 run）
    const replay = await postWithKey(ops, '/v1/ops/graph-builds', { expected_source_revision: 12 }, 'idem_g_1');
    expect(replay.status).toBe(202);
    expect(((await replay.json()) as GraphBuildRun).graph_build_id).toBe(run.graph_build_id);

    const notifications = (await (await jsonRequest(ops, '/v1/notifications')).json()) as {
      items: { type: string; payload: Record<string, unknown> }[];
    };
    expect(
      notifications.items.some(
        (item) => item.type === 'graph_build_completed' && item.payload['status'] === 'succeeded',
      ),
    ).toBe(true);
  });

  it('source_revision 不一致 409；预估不可用 503；公共库为空 422', async () => {
    const ops = bearerOf('ops-wang');
    await expectError(
      await postWithKey(ops, '/v1/ops/graph-builds', { expected_source_revision: 11 }, 'idem_g_3'),
      409,
      'graph_source_changed',
    );

    mockAdmin.setGraphEstimateAvailable(false);
    await expectError(
      await postWithKey(ops, '/v1/ops/graph-builds', { expected_source_revision: 12 }, 'idem_g_4'),
      503,
      'graph_build_estimate_unavailable',
    );
    mockAdmin.setGraphEstimateAvailable(true);

    // 清空公共库文档 → graph_source_empty
    const docs = (await (await jsonRequest(ops, '/v1/spaces/public/documents?page=1&page_size=10')).json()) as {
      items: { id: string; version: number }[];
    };
    for (const doc of docs.items) {
      mockKnowledge.deleteDocument(ops, doc.id, doc.version);
    }
    await expectError(
      await postWithKey(ops, '/v1/ops/graph-builds', { expected_source_revision: 12 }, 'idem_g_5'),
      422,
      'graph_source_empty',
    );
  });

  it('取消：200 + 重复取消返回终态；终态后取消 409；不存在 404', async () => {
    const ops = bearerOf('ops-wang');
    const created = await postWithKey(ops, '/v1/ops/graph-builds', { expected_source_revision: 12 }, 'idem_g_6');
    const run = (await created.json()) as GraphBuildRun;

    const cancelled = await postWithKey(
      ops,
      `/v1/ops/graph-builds/${run.graph_build_id}/cancel`,
      { expected_version: 1 },
      'idem_g_7',
    );
    expect(cancelled.status).toBe(200);
    expect(((await cancelled.json()) as GraphBuildCancelResponse)).toEqual({
      graph_build_id: run.graph_build_id,
      version: 2,
      state: 'cancelled',
    });
    // 重复取消（新键）：返回当前终态而非 409
    const again = await postWithKey(
      ops,
      `/v1/ops/graph-builds/${run.graph_build_id}/cancel`,
      { expected_version: 2 },
      'idem_g_8',
    );
    expect(again.status).toBe(200);
    expect(((await again.json()) as GraphBuildCancelResponse).state).toBe('cancelled');

    await expectError(
      await postWithKey(ops, '/v1/ops/graph-builds/gb_nope/cancel', { expected_version: 1 }, 'idem_g_9'),
      404,
      'graph_build_not_found',
    );
  });

  it('终态（succeeded）run 不可取消：409 graph_build_not_cancellable', async () => {
    const ops = bearerOf('ops-wang');
    const created = await postWithKey(ops, '/v1/ops/graph-builds', { expected_source_revision: 12 }, 'idem_g_a');
    const run = (await created.json()) as GraphBuildRun;
    await jsonRequest(ops, '/v1/ops/graph-builds/current');
    const terminal = (await (await jsonRequest(ops, '/v1/ops/graph-builds/current')).json()) as GraphBuildCurrent;
    expect(terminal.latest_run?.state).toBe('succeeded');
    await expectError(
      await postWithKey(
        ops,
        `/v1/ops/graph-builds/${run.graph_build_id}/cancel`,
        { expected_version: 3 },
        'idem_g_b',
      ),
      409,
      'graph_build_not_cancellable',
    );
  });
});

/* ---------- §10 任务队列 ---------- */

describe('admin contract：任务队列（§10.1–10.2）', () => {
  it('四档视图与 stale_count；ops 行带操作、admin 行空数组', async () => {
    const ops = bearerOf('ops-wang');
    const all = (await (await jsonRequest(ops, '/v1/ops/jobs?view=all')).json()) as OpsJobsResponse;
    expect(all.items.length).toBe(9);
    expect(all.stale_count).toBe(2);
    const active = (await (await jsonRequest(ops, '/v1/ops/jobs?view=active')).json()) as OpsJobsResponse;
    expect(active.items.length).toBe(5);
    // replayable（待人工处理）= failed + dead_letter + cancelled（§10.1 视图定义）
    const replayable = (await (await jsonRequest(ops, '/v1/ops/jobs?view=replayable')).json()) as OpsJobsResponse;
    expect(replayable.items.length).toBe(3);
    const stale = (await (await jsonRequest(ops, '/v1/ops/jobs?view=stale')).json()) as OpsJobsResponse;
    expect(stale.items.length).toBe(2);
    expect(stale.items.every((job) => job.stale)).toBe(true);

    const failed = all.items.find((job) => job.state === 'failed');
    const running = all.items.find((job) => job.state === 'running' && !job.stale);
    expect(failed?.allowed_actions).toEqual(['replay']);
    expect(running?.allowed_actions).toEqual(['cancel']);

    const admin = bearerOf('admin');
    const adminAll = (await (await jsonRequest(admin, '/v1/ops/jobs?view=all')).json()) as OpsJobsResponse;
    expect(adminAll.items.every((job) => job.allowed_actions.length === 0)).toBe(true);
    expect(adminAll.stale_count).toBe(2);
  });

  it('普通用户 403；非法 view 422；cancel 无 key 204；replay 202 语义', async () => {
    const user = bearerOf('zhangsan');
    await expectError(await jsonRequest(user, '/v1/ops/jobs?view=all'), 403, 'ops_jobs_forbidden');
    const ops = bearerOf('ops-wang');
    await expectError(await jsonRequest(ops, '/v1/ops/jobs?view=bogus'), 422, 'validation_error');

    const all = (await (await jsonRequest(ops, '/v1/ops/jobs?view=all')).json()) as OpsJobsResponse;
    const pending = all.items.find((job) => job.state === 'pending');
    const cancel = await jsonRequest(ops, `/v1/ingestion-jobs/${pending?.job_id}/cancel`, { method: 'POST' });
    expect(cancel.status).toBe(204);
    const afterCancel = (await (await jsonRequest(ops, '/v1/ops/jobs?view=all')).json()) as OpsJobsResponse;
    expect(afterCancel.items.find((job) => job.job_id === pending?.job_id)?.state).toBe('cancelled');

    const failed = all.items.find((job) => job.state === 'failed');
    const replay = await postWithKey(ops, `/v1/ingestion-jobs/${failed?.job_id}/replay`, undefined, 'idem_j_1');
    expect(replay.status).toBe(200);
    const afterReplay = (await (await jsonRequest(ops, '/v1/ops/jobs?view=all')).json()) as OpsJobsResponse;
    expect(afterReplay.items.find((job) => job.job_id === failed?.job_id)?.state).toBe('pending');
  });
});

/* ---------- §11 评测与校准 ---------- */

describe('admin contract：评测与校准（§11）', () => {
  it('leaderboard：正式 3 行 / 影子 2 行 / 策略版本；普通用户 403', async () => {
    const ops = bearerOf('ops-wang');
    const body = (await (await jsonRequest(ops, '/v1/evaluation/leaderboard')).json()) as LeaderboardResponse;
    expect(body.entries.length).toBe(3);
    expect(body.shadow_entries.length).toBe(2);
    expect(body.policy.policy_version).toBe('eval_2026_v1');
    expect(body.entries.some((entry) => entry.is_active)).toBe(true);
    const user = bearerOf('zhangsan');
    await expectError(await jsonRequest(user, '/v1/evaluation/leaderboard'), 403, 'evaluation_forbidden');
  });

  it('无窗口时合成 closed 读模型', async () => {
    const ops = bearerOf('ops-wang');
    const body = (await (await jsonRequest(ops, '/v1/calibration/window')).json()) as CalibrationWindow;
    expect(body).toEqual({
      window_id: null,
      status: 'closed',
      opened_at: null,
      closed_at: null,
      pairs_collected: 0,
      close_deadline_at: null,
      window_kind: null,
      policy_version: null,
      sample_rate: 0,
      opened_by: null,
      closed_by: null,
    });
  });

  it('开窗 201 / 状态机 409 系列 / 关窗 200 closing', async () => {
    const ops = bearerOf('ops-wang');
    // open 缺 window_kind：422
    await expectError(
      await postWithKey(ops, '/v1/calibration/window', { action: 'open' }, 'idem_c_1'),
      422,
      'validation_error',
    );
    const opened = await postWithKey(
      ops,
      '/v1/calibration/window',
      { action: 'open', window_kind: 'cold_start' },
      'idem_c_2',
    );
    expect(opened.status).toBe(201);
    const openedBody = (await opened.json()) as CalibrationWindow;
    expect(openedBody).toMatchObject({ status: 'open', window_kind: 'cold_start', sample_rate: 0.4 });

    // 幂等回放：同键同体同 window_id
    const replay = await postWithKey(
      ops,
      '/v1/calibration/window',
      { action: 'open', window_kind: 'cold_start' },
      'idem_c_2',
    );
    expect(((await replay.json()) as CalibrationWindow).window_id).toBe(openedBody.window_id);
    // 同键不同体：409
    await expectError(
      await postWithKey(ops, '/v1/calibration/window', { action: 'open', window_kind: 'manual' }, 'idem_c_2'),
      409,
      'idempotency_key_conflict',
    );

    await expectError(
      await postWithKey(ops, '/v1/calibration/window', { action: 'open', window_kind: 'manual' }, 'idem_c_3'),
      409,
      'calibration_window_already_open',
    );

    const closed = await postWithKey(ops, '/v1/calibration/window', { action: 'close' }, 'idem_c_4');
    expect(closed.status).toBe(200);
    expect(((await closed.json()) as CalibrationWindow).status).toBe('closing');

    await expectError(
      await postWithKey(ops, '/v1/calibration/window', { action: 'open', window_kind: 'manual' }, 'idem_c_5'),
      409,
      'calibration_window_closing',
    );
  });

  it('closed 状态关窗 409 not_open；资格不足 409 not_eligible（manual 豁免）', async () => {
    const ops = bearerOf('ops-wang');
    await expectError(
      await postWithKey(ops, '/v1/calibration/window', { action: 'close' }, 'idem_c_6'),
      409,
      'calibration_window_not_open',
    );
    mockAdmin.setCalibrationEligible(false);
    await expectError(
      await postWithKey(ops, '/v1/calibration/window', { action: 'open', window_kind: 'sentinel' }, 'idem_c_7'),
      409,
      'calibration_window_not_eligible',
    );
    const manual = await postWithKey(
      ops,
      '/v1/calibration/window',
      { action: 'open', window_kind: 'manual' },
      'idem_c_8',
    );
    expect(manual.status).toBe(201);
  });

  it('超管与普通用户写操作 403 forbidden', async () => {
    const admin = bearerOf('admin');
    await expectError(
      await postWithKey(admin, '/v1/calibration/window', { action: 'open', window_kind: 'manual' }, 'idem_c_9'),
      403,
      'forbidden',
    );
    const user = bearerOf('zhangsan');
    await expectError(
      await jsonRequest(user, '/v1/calibration/window'),
      403,
      'calibration_forbidden',
    );
  });
});

/* ---------- §12.1–12.4 用户管理 ---------- */

describe('admin contract：用户管理（§12.1–12.4）', () => {
  it('目录：过滤 / 分页 / 角色可见性', async () => {
    const admin = bearerOf('admin');
    const all = (await (await jsonRequest(admin, '/v1/admin/users?page=1&page_size=20')).json()) as AdminUserListResponse;
    expect(all.total).toBe(9);

    const byQuery = (await (await jsonRequest(admin, '/v1/admin/users?q=zhangsan')).json()) as AdminUserListResponse;
    expect(byQuery.items.map((item) => item.username)).toEqual(['zhangsan']);

    const byDepartment = (await (await jsonRequest(admin, '/v1/admin/users?department_id=d_finance')).json()) as AdminUserListResponse;
    expect(byDepartment.total).toBe(2);

    const byRole = (await (await jsonRequest(admin, '/v1/admin/users?role=user')).json()) as AdminUserListResponse;
    expect(byRole.total).toBe(4);

    const page = (await (await jsonRequest(admin, '/v1/admin/users?page=3&page_size=3')).json()) as AdminUserListResponse;
    expect(page).toMatchObject({ total: 9, page: 3, page_size: 3 });
    expect(page.items.length).toBe(3);

    await expectError(await jsonRequest(admin, '/v1/admin/users?role=bogus'), 422, 'validation_error');
    const ops = bearerOf('ops-wang');
    expect((await jsonRequest(ops, '/v1/admin/users')).status).toBe(200);
    const user = bearerOf('zhangsan');
    await expectError(await jsonRequest(user, '/v1/admin/users'), 403, 'admin_users_forbidden');
  });

  it('创建 201 与 409/422/404/403 系列', async () => {
    const admin = bearerOf('admin');
    const created = await jsonRequest(admin, '/v1/admin/users', {
      method: 'POST',
      body: {
        username: 'newhire',
        real_name: 'New Hire',
        department_id: 'd_hr',
        role: 'user',
        initial_password: 'password123',
      },
    });
    expect(created.status).toBe(201);
    expect(((await created.json()) as AdminUserItem)).toMatchObject({
      username: 'newhire',
      role: 'user',
      version: 1,
      lifecycle_status: 'active',
    });

    const base = { real_name: 'X', department_id: null, role: 'user', initial_password: 'password123' };
    await expectError(
      await jsonRequest(admin, '/v1/admin/users', { method: 'POST', body: { ...base, username: 'zhangsan' } }),
      409,
      'username_exists',
    );
    await expectError(
      await jsonRequest(admin, '/v1/admin/users', {
        method: 'POST',
        body: { ...base, username: 'r_admin', role: 'admin' },
      }),
      403,
      'forbidden_target',
    );
    await expectError(
      await jsonRequest(admin, '/v1/admin/users', {
        method: 'POST',
        body: { ...base, username: 'r_minister', role: 'minister' },
      }),
      422,
      'minister_department_required',
    );
    await expectError(
      await jsonRequest(admin, '/v1/admin/users', {
        method: 'POST',
        body: { ...base, username: 'r_legacy', department_id: 'd_legacy' },
      }),
      409,
      'department_inactive',
    );
    await expectError(
      await jsonRequest(admin, '/v1/admin/users', {
        method: 'POST',
        body: { ...base, username: 'r_nope', department_id: 'd_nope' },
      }),
      404,
      'department_not_found',
    );
    // 运维不可创建 ops 角色
    const ops = bearerOf('ops-wang');
    await expectError(
      await jsonRequest(ops, '/v1/admin/users', {
        method: 'POST',
        body: { ...base, username: 'r_ops', role: 'ops' },
      }),
      403,
      'forbidden_target',
    );
  });

  it('编辑：版本冲突 / 显式 null 解绑 / 目标守卫三态', async () => {
    const admin = bearerOf('admin');
    const patched = await jsonRequest(admin, '/v1/admin/users/u_chen', {
      method: 'PATCH',
      body: { expected_version: 1, role: 'minister', department_id: 'd_hr' },
    });
    expect(patched.status).toBe(200);
    expect(((await patched.json()) as AdminUserItem)).toMatchObject({ role: 'minister', version: 2 });

    await expectError(
      await jsonRequest(admin, '/v1/admin/users/u_chen', {
        method: 'PATCH',
        body: { expected_version: 1, role: 'user' },
      }),
      409,
      'version_conflict',
    );

    // 显式 department_id: null 解除部门归属
    const unbound = await jsonRequest(admin, '/v1/admin/users/u_chen', {
      method: 'PATCH',
      body: { expected_version: 2, role: 'user', department_id: null },
    });
    expect(((await unbound.json()) as AdminUserItem).department).toBeNull();

    // admin 目标 / 自身 / 冻结目标
    await expectError(
      await jsonRequest(admin, '/v1/admin/users/u_admin', {
        method: 'PATCH',
        body: { expected_version: 1, role: 'user' },
      }),
      403,
      'forbidden_target',
    );
    const ops = bearerOf('ops-wang');
    await expectError(
      await jsonRequest(ops, '/v1/admin/users/u_ops', {
        method: 'PATCH',
        body: { expected_version: 1, role: 'user' },
      }),
      403,
      'cannot_modify_self',
    );
    await expectError(
      await jsonRequest(admin, '/v1/admin/users/u_ghost', {
        method: 'PATCH',
        body: { expected_version: 1, role: 'minister' },
      }),
      409,
      'user_pending_delete',
    );
  });

  it('永久禁用 202：pending_delete + purge_after_at；重复禁用 409', async () => {
    const admin = bearerOf('admin');
    const deleted = await jsonRequest(admin, '/v1/admin/users/u_zhao', {
      method: 'DELETE',
      body: { expected_version: 1 },
    });
    expect(deleted.status).toBe(202);
    const body = (await deleted.json()) as {
      lifecycle_status: string;
      purge_after_at: string | null;
      version: number;
    };
    expect(body.lifecycle_status).toBe('pending_delete');
    expect(body.purge_after_at).not.toBeNull();

    await expectError(
      await jsonRequest(admin, '/v1/admin/users/u_zhao', {
        method: 'DELETE',
        body: { expected_version: 2 },
      }),
      409,
      'user_pending_delete',
    );
  });
});

/* ---------- §12.5–12.6 部门管理 ---------- */

describe('admin contract：部门管理（§12.5–12.6）', () => {
  it('目录：三档过滤与 allowed_actions 角色差异', async () => {
    const admin = bearerOf('admin');
    const active = (await (await jsonRequest(admin, '/v1/admin/departments?status=active')).json()) as AdminDepartmentListResponse;
    expect(active.items.length).toBe(3);
    expect(active.items.every((item) => item.allowed_actions.length === 2)).toBe(true);
    const inactive = (await (await jsonRequest(admin, '/v1/admin/departments?status=inactive')).json()) as AdminDepartmentListResponse;
    expect(inactive.items.map((item) => item.name)).toEqual(['档案部']);
    expect(inactive.items[0]?.allowed_actions).toEqual([]);
    const all = (await (await jsonRequest(admin, '/v1/admin/departments?status=all')).json()) as AdminDepartmentListResponse;
    expect(all.items.length).toBe(4);

    const ops = bearerOf('ops-wang');
    const opsList = (await (await jsonRequest(ops, '/v1/admin/departments?status=all')).json()) as AdminDepartmentListResponse;
    expect(opsList.items.every((item) => item.allowed_actions.length === 0)).toBe(true);
    await expectError(
      await jsonRequest(admin, '/v1/admin/departments?status=bogus'),
      422,
      'validation_error',
    );
  });

  it('创建 201 + 幂等回放 + 重名 409 + 运维 403', async () => {
    const admin = bearerOf('admin');
    const created = await postWithKey(admin, '/v1/admin/departments', { name: 'Brand New Dept' }, 'idem_d_1');
    expect(created.status).toBe(201);
    const createdBody = (await created.json()) as AdminDepartmentItem;
    expect(createdBody).toMatchObject({ name: 'Brand New Dept', status: 'active', version: 1 });

    const replay = await postWithKey(admin, '/v1/admin/departments', { name: 'Brand New Dept' }, 'idem_d_1');
    expect(((await replay.json()) as AdminDepartmentItem).id).toBe(createdBody.id);
    const all = (await (await jsonRequest(admin, '/v1/admin/departments?status=all')).json()) as AdminDepartmentListResponse;
    expect(all.items.length).toBe(5);

    await expectError(
      await postWithKey(admin, '/v1/admin/departments', { name: '财务部' }, 'idem_d_2'),
      409,
      'department_name_exists',
    );
    const ops = bearerOf('ops-wang');
    await expectError(
      await postWithKey(ops, '/v1/admin/departments', { name: 'Ops Dept' }, 'idem_d_3'),
      403,
      'department_action_forbidden',
    );
    await expectError(
      await jsonRequest(admin, '/v1/admin/departments', { method: 'POST', body: { name: 'No Key' } }),
      422,
      'validation_error',
    );
  });

  it('改名：version_conflict 带 current_version；成功 200', async () => {
    const admin = bearerOf('admin');
    const conflictResponse = await jsonRequest(admin, '/v1/admin/departments/d_empty', {
      method: 'PATCH',
      body: { expected_version: 9, name: '空壳部二' },
      idempotencyKey: 'idem_d_4',
    });
    const conflict = await expectError(conflictResponse, 409, 'version_conflict');
    expect(conflict.error.details['current_version']).toBe(1);

    const renamed = await jsonRequest(admin, '/v1/admin/departments/d_empty', {
      method: 'PATCH',
      body: { expected_version: 1, name: '空壳部二' },
      idempotencyKey: 'idem_d_5',
    });
    expect(renamed.status).toBe(200);
    expect(((await renamed.json()) as AdminDepartmentItem)).toMatchObject({
      name: '空壳部二',
      version: 2,
    });
  });

  it('停用：阻断三兄弟 / 503 可重试夹具 / 成功与重复停用', async () => {
    const admin = bearerOf('admin');
    await expectError(
      await postWithKey(admin, '/v1/admin/departments/d_finance/deactivate', { expected_version: 1 }, 'idem_d_6'),
      409,
      'department_has_members',
    );
    await expectError(
      await postWithKey(admin, '/v1/admin/departments/d_hr/deactivate', { expected_version: 1 }, 'idem_d_7'),
      409,
      'department_has_active_work',
    );

    mockAdmin.setDepartmentDeactivationUnverified('d_empty', true);
    const unverified = await expectError(
      await postWithKey(admin, '/v1/admin/departments/d_empty/deactivate', { expected_version: 1 }, 'idem_d_8'),
      503,
      'department_deactivation_unverified',
    );
    expect(unverified.error.details['retryable']).toBe(true);

    mockAdmin.setDepartmentDeactivationUnverified('d_empty', false);
    const deactivated = await postWithKey(
      admin,
      '/v1/admin/departments/d_empty/deactivate',
      { expected_version: 1 },
      'idem_d_9',
    );
    expect(deactivated.status).toBe(200);
    expect(((await deactivated.json()) as AdminDepartmentItem)).toMatchObject({
      status: 'inactive',
      version: 2,
    });
    await expectError(
      await postWithKey(admin, '/v1/admin/departments/d_empty/deactivate', { expected_version: 2 }, 'idem_d_a'),
      409,
      'department_inactive',
    );
  });
});

/* ---------- §12.7 权限矩阵 ---------- */

describe('admin contract：权限矩阵（§12.7）', () => {
  it('超管 200 ≥10 行且四角色键齐备；其余角色 403', async () => {
    const admin = bearerOf('admin');
    const response = await jsonRequest(admin, '/v1/admin/permission-matrix');
    expect(response.status).toBe(200);
    const body = (await response.json()) as PermissionMatrixResponse;
    expect(body.capabilities.length).toBeGreaterThanOrEqual(10);
    for (const row of body.capabilities) {
      expect(Object.keys(row.roles).sort()).toEqual(['admin', 'minister', 'ops', 'user']);
    }
    const ops = bearerOf('ops-wang');
    await expectError(
      await jsonRequest(ops, '/v1/admin/permission-matrix'),
      403,
      'permission_matrix_forbidden',
    );
  });
});
