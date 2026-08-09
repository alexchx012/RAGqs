import { describe, expect, it } from 'vitest';
import { resolveUrl } from '../api/client';
import type { SubmissionStatus } from '../settings/types';
import { MockHttpError } from './auth-contract';
import { mockAuth, mockKnowledge, mockNotifications, mockQuota } from './testing';

function bearerOf(username = 'zhangsan', password = 'password123'): string {
  const { accessToken } = mockAuth.login(username, password, 'vitest');
  return `Bearer ${accessToken}`;
}

function expectHttpError(fn: () => unknown, status: number, code: string): void {
  try {
    fn();
  } catch (error) {
    expect(error).toBeInstanceOf(MockHttpError);
    const httpError = error as MockHttpError;
    expect(httpError.status).toBe(status);
    expect(httpError.code).toBe(code);
    return;
  }
  throw new Error(`expected MockHttpError ${status} ${code}`);
}

async function uploadRequest(
  token: string,
  spaceId: string,
  files: { name: string; type: string; content: string }[],
  idempotencyKey: string,
): Promise<Response> {
  // 与生产 api.uploadDocuments 相同的 wire 格式：字节级 multipart 保留真实文件名
  //（jsdom FormData 经 undici 序列化会把 File 名抹成 'blob'，测试路径必须与生产一致）。
  const boundary = '----RAGqsTestBoundary7MA4YWxk';
  const encoder = new TextEncoder();
  const chunks: Uint8Array[] = [];
  for (const file of files) {
    chunks.push(
      encoder.encode(
        `--${boundary}\r\nContent-Disposition: form-data; name="files"; filename="${file.name}"\r\n` +
          `Content-Type: ${file.type || 'application/octet-stream'}\r\n\r\n`,
      ),
      encoder.encode(file.content),
      encoder.encode('\r\n'),
    );
  }
  chunks.push(encoder.encode(`--${boundary}--\r\n`));
  const length = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const body = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.length;
  }
  return fetch(resolveUrl(`/v1/spaces/${encodeURIComponent(spaceId)}/documents`), {
    method: 'POST',
    headers: {
      Authorization: token,
      'Content-Type': `multipart/form-data; boundary=${boundary}`,
      'Idempotency-Key': idempotencyKey,
    },
    body,
  });
}

async function listDocuments(token: string, spaceId: string, q?: string, page = 1, pageSize = 10): Promise<Response> {
  const params = new URLSearchParams();
  if (q !== undefined) params.set('q', q);
  params.set('page', String(page));
  params.set('page_size', String(pageSize));
  return fetch(resolveUrl(`/v1/spaces/${encodeURIComponent(spaceId)}/documents?${params}`), {
    headers: { Authorization: token },
  });
}

async function listJobs(token: string, limit?: number): Promise<Response> {
  const query = limit === undefined ? '' : `?limit=${limit}`;
  return fetch(resolveUrl(`/v1/ingestion-jobs${query}`), { headers: { Authorization: token } });
}

async function listSubmissions(token: string, status: SubmissionStatus | 'all' = 'all'): Promise<Response> {
  const query = status === 'all' ? '' : `?status=${status}`;
  return fetch(resolveUrl(`/v1/submissions${query}`), { headers: { Authorization: token } });
}

describe('knowledge contract mock：文档列表与分页', () => {
  it('个人库默认上传时间倒序，q 按文件名过滤，page/page_size 分页', async () => {
    const token = bearerOf('zhangsan');
    const first = await listDocuments(token, 'personal:u_user', undefined, 1, 2);
    expect(first.status).toBe(200);
    const firstBody = (await first.json()) as { items: { name: string }[]; total: number; page: number; page_size: number };
    expect(firstBody.total).toBe(3);
    expect(firstBody.page).toBe(1);
    expect(firstBody.page_size).toBe(2);
    expect(firstBody.items.map((item) => item.name)).toEqual(['员工手册.pdf', '报销制度.docx']);

    const second = await listDocuments(token, 'personal:u_user', undefined, 2, 2);
    const secondBody = (await second.json()) as { items: { name: string }[] };
    expect(secondBody.items.map((item) => item.name)).toEqual(['年假政策.md']);

    const filtered = await listDocuments(token, 'personal:u_user', '报销');
    const filteredBody = (await filtered.json()) as { items: { name: string }[] };
    expect(filteredBody.items.map((item) => item.name)).toEqual(['报销制度.docx']);
  });

  it('read-only 空间无管理入口的契约：列表可读但操作端点在服务端受权限约束', async () => {
    const token = bearerOf('zhangsan');
    // 普通用户可读本部门库（read），但列表照常返回
    const response = await listDocuments(token, 'department:d_finance');
    expect(response.status).toBe(200);
    // 不可读空间 404
    const forbidden = await listDocuments(token, 'department:d_hr');
    expect(forbidden.status).toBe(404);
  });
});

describe('knowledge contract mock：上传三分支', () => {
  it('manage 目标多文件：逐文件结果 + 批次 + 任务卡；dedupe 基于内容 hash（review C12）', async () => {
    const token = bearerOf('zhangsan');
    const response = await uploadRequest(
      token,
      'personal:u_user',
      [
        { name: '季度总结.pdf', type: 'application/pdf', content: '%PDF-1.4' },
        // 内容与上一文件相同（hash 相同）→ deduplicated；文件名不同不影响判定
        { name: '员工手册-副本.pdf', type: 'application/pdf', content: '%PDF-1.4' },
      ],
      'idem-upload-1',
    );
    expect(response.status).toBe(200);
    const body = (await response.json()) as {
      upload_batch_id: string | null;
      items: { name: string; accepted: boolean; deduplicated?: boolean; job_id: string | null; submission_id?: string }[];
    };
    expect(body.upload_batch_id).toBeTruthy();
    expect(body.items).toHaveLength(2);
    expect(body.items[0]).toMatchObject({ name: '季度总结.pdf', accepted: true });
    expect(body.items[0].deduplicated).toBeUndefined();
    expect(body.items[0].job_id).toBeTruthy();
    expect(body.items[1]).toMatchObject({ name: '员工手册-副本.pdf', accepted: true, deduplicated: true });
    expect(body.items[1].job_id).toBeNull();

    const jobs = (await (await listJobs(token)).json()) as { items: { job_id: string; state: string; name: string }[] };
    expect(jobs.items.some((job) => job.name === '季度总结.pdf')).toBe(true);
    // 批次统计：rejected=0、deduplicated=1（全部文件结果保留在 items）
    const batch = (await (await fetch(resolveUrl(`/v1/upload-batches/${body.upload_batch_id}`), { headers: { Authorization: token } })).json()) as {
      summary: { rejected: number; deduplicated: number; total_files: number };
    };
    expect(batch.summary).toMatchObject({ rejected: 0, deduplicated: 1, total_files: 2 });
  });

  it('失败项按服务端错误对象呈现（不预拒），含 413/422/415 映射', async () => {
    const token = bearerOf('zhangsan');
    mockKnowledge.setNextUploadFailure('big', 'upload_too_large');
    mockKnowledge.setNextUploadFailure('virus', 'malware_detected');
    const response = await uploadRequest(
      token,
      'personal:u_user',
      [
        { name: 'big-file.pdf', type: 'application/pdf', content: '%PDF' },
        { name: 'virus.pdf', type: 'application/pdf', content: '%PDF' },
        { name: 'notes.zip', type: 'application/zip', content: 'PK' },
        { name: 'weird.xyz', type: 'application/x-custom', content: 'x' },
      ],
      'idem-upload-2',
    );
    expect(response.status).toBe(200);
    const body = (await response.json()) as { items: { name: string; accepted: boolean; error?: { code: string } }[] };
    const byName = new Map(body.items.map((item) => [item.name, item]));
    expect(byName.get('big-file.pdf')).toMatchObject({ accepted: false, error: { code: 'upload_too_large' } });
    expect(byName.get('virus.pdf')).toMatchObject({ accepted: false, error: { code: 'malware_detected' } });
    expect(byName.get('notes.zip')).toMatchObject({ accepted: false, error: { code: 'unsafe_archive' } });
    expect(byName.get('weird.xyz')).toMatchObject({ accepted: false, error: { code: 'unsupported_media_type' } });
  });

  it('quota_exceeded 整批拒绝：不预扣不冻结，配额未变化', async () => {
    const token = bearerOf('zhangsan');
    const before = mockQuota.snapshot('u_user');
    expect(before.used).toBe(120);
    mockQuota.raiseUsage('u_user', 500 - before.used - 5); // 只剩 5 页
    const response = await uploadRequest(
      token,
      'personal:u_user',
      [{ name: '页10报告.pdf', type: 'application/pdf', content: '%PDF' }],
      'idem-upload-3',
    );
    expect(response.status).toBe(409);
    const body = (await response.json()) as { error: { code: string } };
    expect(body.error.code).toBe('quota_exceeded');
    // 只加了夹具（120+375=495），上传未再扣；未创建任何任务卡
    expect(mockQuota.snapshot('u_user').used).toBe(495);
    expect((await (await listJobs(token)).json()).items.some((job: { name: string }) => job.name === '页10报告.pdf')).toBe(false);
  });

  it('contribute 目标：创建投稿、quota_exempt、不检查页额度', async () => {
    const token = bearerOf('zhangsan');
    const response = await uploadRequest(
      token,
      'public',
      [{ name: '公共建议稿.md', type: 'text/markdown', content: '# 建议' }],
      'idem-upload-4',
    );
    expect(response.status).toBe(200);
    const body = (await response.json()) as { upload_batch_id: string | null; items: { submission_id?: string; quota_exempt?: boolean }[] };
    expect(body.upload_batch_id).toBeNull();
    expect(body.items[0]?.submission_id).toBeTruthy();
    expect(body.items[0]?.quota_exempt).toBe(true);

    const submissions = (await (await listSubmissions(token, 'pending')).json()) as {
      items: { name: string; status: string }[];
    };
    expect(submissions.items.some((item) => item.name === '公共建议稿.md')).toBe(true);
  });
});

describe('knowledge contract mock：任务状态机与批次', () => {
  it('任务推进到 succeeded 登记可 ack 事件；批次 partial 仅批次级', async () => {
    const token = bearerOf('zhangsan');
    const upload = await uploadRequest(
      token,
      'personal:u_user',
      [{ name: '任务推进文档.pdf', type: 'application/pdf', content: '%PDF' }],
      'idem-job-1',
    );
    const body = (await upload.json()) as { upload_batch_id: string; items: { job_id: string }[] };
    const jobId = body.items[0]?.job_id ?? '';
    expect(jobId).toBeTruthy();

    const batchBefore = (await (await fetch(resolveUrl(`/v1/upload-batches/${body.upload_batch_id}`), { headers: { Authorization: token } })).json()) as { state: string };
    expect(batchBefore.state).toBe('partial');

    mockKnowledge.advanceJob(token, jobId, 'running', 'parsing');
    const running = (await (await listJobs(token)).json()) as { items: { job_id: string; stage: string | null; state: string }[] };
    expect(running.items.find((job) => job.job_id === jobId)).toMatchObject({ state: 'running', stage: 'parsing' });

    mockKnowledge.advanceJob(token, jobId, 'succeeded');
    const succeeded = (await (await listJobs(token)).json()) as {
      items: { job_id: string; state: string; usage: { pages: number } | null; notification_event_ids: string[] }[];
    };
    const done = succeeded.items.find((job) => job.job_id === jobId);
    expect(done?.state).toBe('succeeded');
    expect(done?.notification_event_ids.length).toBeGreaterThan(0);

    // ack 前未读增加；ack 后未读数回落
    const unreadBefore = mockNotifications.unreadCount(token);
    const eventId = done?.notification_event_ids[0] ?? '';
    mockNotifications.ack(token, eventId);
    expect(mockNotifications.unreadCount(token)).toBe(Math.max(0, unreadBefore - 1));
  });

  it('allowed_actions 唯一依据 + ACL：普通用户只见 cancel，replay 仅 ops/admin（review C13）', async () => {
    const token = bearerOf('zhangsan');
    const upload = await uploadRequest(
      token,
      'personal:u_user',
      [{ name: '动作推导.pdf', type: 'application/pdf', content: '%PDF' }],
      'idem-job-2',
    );
    const body = (await upload.json()) as { items: { job_id: string }[] };
    const jobId = body.items[0]?.job_id ?? '';

    mockKnowledge.setRetryWait(token, jobId, new Date(Date.now() + 60_000).toISOString());
    const retry = (await (await listJobs(token)).json()) as { items: { job_id: string; allowed_actions: string[] }[] };
    // 普通用户：replay 不出现在 allowed_actions（ACL）
    expect(retry.items.find((job) => job.job_id === jobId)?.allowed_actions).toEqual(['cancel']);
    // 普通用户直接调 replay → 403
    expectHttpError(() => mockKnowledge.replayJob(token, jobId, 'idem-replay-user'), 403, 'job_replay_forbidden');

    // ops 视角：retry_wait 可见 cancel+replay
    const opsToken = bearerOf('ops-wang');
    const opsRetry = (await (await listJobs(opsToken)).json()) as { items: { job_id: string; allowed_actions: string[] }[] };
    expect(opsRetry.items.find((job) => job.job_id === jobId)?.allowed_actions).toEqual(['cancel', 'replay']);

    mockKnowledge.failJob(token, jobId, 'table_parse_failed');
    const failed = (await (await listJobs(opsToken)).json()) as { items: { job_id: string; allowed_actions: string[]; failure_reason: string | null }[] };
    expect(failed.items.find((job) => job.job_id === jobId)?.allowed_actions).toEqual(['replay']);
    expect(failed.items.find((job) => job.job_id === jobId)?.failure_reason).toBe('table_parse_failed');

    // failed 状态：cancel 不在 allowed_actions → 409；ops replay 允许 → 成功回到 pending/queued
    expectHttpError(() => mockKnowledge.cancelJob(opsToken, jobId), 409, 'job_state_conflict');
    mockKnowledge.replayJob(opsToken, jobId, 'idem-replay');
    const replayed = (await (await listJobs(opsToken)).json()) as { items: { job_id: string; state: string; stage: string | null; allowed_actions: string[] }[] };
    expect(replayed.items.find((job) => job.job_id === jobId)).toMatchObject({
      state: 'pending',
      stage: 'queued',
      // pending 状态：allowed_actions 仅 cancel（replay 只在 retry_wait/failed/dead_letter）
      allowed_actions: ['cancel'],
    });
  });
});

describe('knowledge contract mock：版本记录', () => {
  it('恢复创建新版本与新任务；409 document_version_purged 后内容不可用', async () => {
    const token = bearerOf('zhangsan');
    const docs = (await (await listDocuments(token, 'personal:u_user')).json()) as {
      items: { id: string; document_version_id: string; version: number }[];
    };
    const target = docs.items[0];
    const versions = (await (await fetch(resolveUrl(`/v1/documents/${target.id}/versions`), { headers: { Authorization: token } })).json()) as {
      items: { document_version_id: string; status: string; content_available: boolean }[];
      version: number;
    };
    expect(versions.items[0]?.status).toBe('active');

    const restore = await fetch(
      resolveUrl(`/v1/documents/${target.id}/versions/${target.document_version_id}/restore`),
      {
        method: 'POST',
        headers: { Authorization: token, 'Content-Type': 'application/json', 'Idempotency-Key': 'idem-restore-1' },
        body: JSON.stringify({ expected_version: versions.version }),
      },
    );
    expect(restore.status).toBe(200);
    const restored = (await restore.json()) as { job_id: string; document_version_id: string };
    expect(restored.job_id).toBeTruthy();
    expect(restored.document_version_id).not.toBe(target.document_version_id);

    // 行版本已推进 → 旧 expected_version 触发 409
    const conflict = await fetch(resolveUrl(`/v1/documents/${target.id}/versions/${target.document_version_id}/restore`), {
      method: 'POST',
      headers: { Authorization: token, 'Content-Type': 'application/json', 'Idempotency-Key': 'idem-restore-2' },
      body: JSON.stringify({ expected_version: versions.version }),
    });
    expect(conflict.status).toBe(409);
    expect(((await conflict.json()) as { error: { code: string } }).error.code).toBe('document_version_purged');
  });

  it('重建索引与删除：expected_version 冲突 409；202 后立即移除', async () => {
    const token = bearerOf('zhangsan');
    const docs = (await (await listDocuments(token, 'personal:u_user')).json()) as {
      items: { id: string; version: number }[];
    };
    const target = docs.items[0];

    const reindex = await fetch(resolveUrl(`/v1/documents/${target.id}/reindex`), {
      method: 'POST',
      headers: { Authorization: token, 'Content-Type': 'application/json', 'Idempotency-Key': 'idem-reindex-1' },
      body: JSON.stringify({ expected_version: target.version }),
    });
    expect(reindex.status).toBe(200);
    expect(((await reindex.json()) as { job_id: string }).job_id).toBeTruthy();

    const del = await fetch(resolveUrl(`/v1/documents/${target.id}`), {
      method: 'DELETE',
      headers: { Authorization: token, 'Content-Type': 'application/json', 'Idempotency-Key': 'idem-del-1' },
      body: JSON.stringify({ expected_version: target.version }),
    });
    expect(del.status).toBe(202);
    const after = (await (await listDocuments(token, 'personal:u_user')).json()) as { items: { id: string }[] };
    expect(after.items.some((item) => item.id === target.id)).toBe(false);
  });
});

describe('knowledge contract mock：投稿五态与 409', () => {
  it('撤回后行保留转已撤回；409 version_conflict 与 state_conflict', async () => {
    const token = bearerOf('zhangsan');
    const upload = await uploadRequest(
      token,
      'public',
      [{ name: '待撤回投稿.md', type: 'text/markdown', content: '# x' }],
      'idem-sub-1',
    );
    const body = (await upload.json()) as { items: { submission_id: string; version: number }[] };
    const submissionId = body.items[0]?.submission_id ?? '';
    const version = body.items[0]?.version ?? 1;

    const withdraw = await fetch(resolveUrl(`/v1/submissions/${submissionId}/withdraw`), {
      method: 'POST',
      headers: { Authorization: token, 'Content-Type': 'application/json', 'Idempotency-Key': 'idem-withdraw-1' },
      body: JSON.stringify({ expected_version: version }),
    });
    expect(withdraw.status).toBe(200);
    expect(((await withdraw.json()) as { status: string }).status).toBe('withdrawn');

    // 已撤回再撤回 → state_conflict
    const again = await fetch(resolveUrl(`/v1/submissions/${submissionId}/withdraw`), {
      method: 'POST',
      headers: { Authorization: token, 'Content-Type': 'application/json', 'Idempotency-Key': 'idem-withdraw-2' },
      body: JSON.stringify({ expected_version: version + 1 }),
    });
    expect(again.status).toBe(409);
    expect(((await again.json()) as { error: { code: string } }).error.code).toBe('submission_state_conflict');

    const submissions = (await (await listSubmissions(token, 'withdrawn')).json()) as { items: { submission_id: string; status: string }[] };
    expect(submissions.items.some((item) => item.submission_id === submissionId)).toBe(true);
  });

  it('rejected 可删除（204 后移除）；pending/approved 删除 409', async () => {
    const submitterToken = bearerOf('zhangsan');
    const ministerToken = bearerOf('minister-li');
    // 复用种子待审核投稿（zhangsan → 财务部），先经部长驳回
    const list = (await (await fetch(resolveUrl('/v1/approvals/submissions'), { headers: { Authorization: ministerToken } })).json()) as {
      items: { submission_id: string; version: number }[];
    };
    const target = list.items[0];
    expect(target).toBeDefined();

    // pending 删除 → 409
    const pendingDel = await fetch(resolveUrl(`/v1/submissions/${target.submission_id}`), {
      method: 'DELETE',
      headers: { Authorization: submitterToken, 'Content-Type': 'application/json', 'Idempotency-Key': 'idem-del-pending' },
      body: JSON.stringify({ expected_version: target.version }),
    });
    expect(pendingDel.status).toBe(409);
    expect(((await pendingDel.json()) as { error: { code: string } }).error.code).toBe('submission_state_conflict');

    // 部长驳回（固定原因）→ 投稿人行仍保留、状态 rejected
    const reject = await fetch(resolveUrl(`/v1/approvals/submissions/${target.submission_id}/reject`), {
      method: 'POST',
      headers: { Authorization: ministerToken, 'Content-Type': 'application/json', 'Idempotency-Key': 'idem-reject-del' },
      body: JSON.stringify({ expected_version: target.version }),
    });
    expect(reject.status).toBe(200);
    const rejected = (await reject.json()) as { version: number };

    // rejected 删除 → 204
    const del = await fetch(resolveUrl(`/v1/submissions/${target.submission_id}`), {
      method: 'DELETE',
      headers: { Authorization: submitterToken, 'Content-Type': 'application/json', 'Idempotency-Key': 'idem-del-final' },
      body: JSON.stringify({ expected_version: rejected.version }),
    });
    expect(del.status).toBe(204);
    const remaining = (await (await listSubmissions(submitterToken)).json()) as { items: { submission_id: string }[] };
    expect(remaining.items.some((item) => item.submission_id === target.submission_id)).toBe(false);
  });

  it('查看内容：404 submission_content_unavailable 后不返回内容', async () => {
    const token = bearerOf('zhangsan');
    const upload = await uploadRequest(
      token,
      'public',
      [{ name: '内容过期投稿.md', type: 'text/markdown', content: '# x' }],
      'idem-sub-3',
    );
    const body = (await upload.json()) as { items: { submission_id: string }[] };
    const submissionId = body.items[0]?.submission_id ?? '';

    const ok = await fetch(resolveUrl(`/v1/submissions/${submissionId}/content`), { headers: { Authorization: token } });
    expect(ok.status).toBe(200);

    mockKnowledge.expireSubmissionContent(token, submissionId);
    const gone = await fetch(resolveUrl(`/v1/submissions/${submissionId}/content`), { headers: { Authorization: token } });
    expect(gone.status).toBe(404);
    expect(((await gone.json()) as { error: { code: string } }).error.code).toBe('submission_content_unavailable');
  });
});

describe('knowledge contract mock：部长部门库审核', () => {
  it('审批徽标按本部门 pending 计数；通过后投稿人侧转 approved 且铃铛送达', async () => {
    const ministerToken = bearerOf('minister-li');
    const submitterToken = bearerOf('zhangsan');

    const summary = (await (await fetch(resolveUrl('/v1/approvals/summary'), { headers: { Authorization: ministerToken } })).json()) as {
      submission_pending: number;
    };
    expect(summary.submission_pending).toBeGreaterThan(0);

    const list = (await (await fetch(resolveUrl('/v1/approvals/submissions'), { headers: { Authorization: ministerToken } })).json()) as {
      items: { submission_id: string; version: number; name: string }[];
    };
    expect(list.items.length).toBe(summary.submission_pending);
    const target = list.items[0];

    const approve = await fetch(resolveUrl(`/v1/approvals/submissions/${target.submission_id}/approve`), {
      method: 'POST',
      headers: { Authorization: ministerToken, 'Content-Type': 'application/json', 'Idempotency-Key': 'idem-approve-1' },
      body: JSON.stringify({ expected_version: target.version }),
    });
    expect(approve.status).toBe(200);
    expect(((await approve.json()) as { status: string; job_id?: string }).status).toBe('approved');

    // 投稿人侧联动
    const mine = (await (await listSubmissions(submitterToken)).json()) as { items: { name: string; status: string }[] };
    expect(mine.items.find((item) => item.name === target.name)?.status).toBe('approved');

    // 铃铛送达（未读数含 submission_approved）
    expect(mockNotifications.unreadCount(submitterToken)).toBeGreaterThan(0);
  });

  it('驳回带原因送达投稿人；version_conflict 刷新后重试', async () => {
    const ministerToken = bearerOf('minister-li');
    const submitterToken = bearerOf('zhangsan');
    const list = (await (await fetch(resolveUrl('/v1/approvals/submissions'), { headers: { Authorization: ministerToken } })).json()) as {
      items: { submission_id: string; version: number }[];
    };
    const target = list.items[list.items.length - 1];
    expect(target).toBeDefined();

    // 陈旧版本（仍为合法整数、状态仍 pending）→ 409 version_conflict
    const stale = await fetch(resolveUrl(`/v1/approvals/submissions/${target.submission_id}/reject`), {
      method: 'POST',
      headers: { Authorization: ministerToken, 'Content-Type': 'application/json', 'Idempotency-Key': 'idem-reject-stale' },
      body: JSON.stringify({ expected_version: target.version + 1 }),
    });
    expect(stale.status).toBe(409);
    expect(((await stale.json()) as { error: { code: string } }).error.code).toBe('version_conflict');

    const reject = await fetch(resolveUrl(`/v1/approvals/submissions/${target.submission_id}/reject`), {
      method: 'POST',
      headers: { Authorization: ministerToken, 'Content-Type': 'application/json', 'Idempotency-Key': 'idem-reject-1' },
      body: JSON.stringify({ expected_version: target.version, reason: '格式不符合要求' }),
    });
    expect(reject.status).toBe(200);
    expect(((await reject.json()) as { status: string }).status).toBe('rejected');

    const mine = (await (await listSubmissions(submitterToken, 'rejected')).json()) as {
      items: { name: string; status: string; reject_reason: string | null }[];
    };
    const rejected = mine.items.find((item) => item.status === 'rejected');
    expect(rejected).toBeDefined();
    expect(rejected?.reject_reason).toBe('格式不符合要求');
    expect(mockNotifications.unreadCount(submitterToken)).toBeGreaterThan(0);
  });

  it('非部长/非本部门不可审批：403 approval_forbidden', () => {
    const userToken = bearerOf('zhangsan');
    const list = mockKnowledge.listApprovals(bearerOf('minister-li'));
    const target = list.items[0];
    expectHttpError(
      () => mockKnowledge.approveSubmission(userToken, target.submission_id, target.version),
      403,
      'approval_forbidden',
    );
  });
});

describe('knowledge contract mock：上传新版本（§6.4）与幂等回放', () => {
  it('上传新版本：固定目标 document_id + expected_version，任务 succeeded 前旧 active 继续服务', async () => {
    const token = bearerOf('zhangsan');
    const docs = (await (await listDocuments(token, 'personal:u_user')).json()) as {
      items: { id: string; version: number; document_version_id: string }[];
    };
    const target = docs.items[0];
    const before = (await (await fetch(resolveUrl(`/v1/documents/${target.id}/versions`), { headers: { Authorization: token } })).json()) as {
      active_version_id: string | null;
      items: { document_version_id: string; status: string }[];
    };
    expect(before.active_version_id).toBe(target.document_version_id);

    // 新版本 multipart：files（单文件）+ expected_version 表单字段
    const boundary = '----RAGqsNewVersionBoundary42';
    const encoder = new TextEncoder();
    const chunks = [
      encoder.encode(`--${boundary}\r\nContent-Disposition: form-data; name="files"; filename="员工手册-v2.pdf"\r\nContent-Type: application/pdf\r\n\r\n`),
      encoder.encode('%PDF-1.4'),
      encoder.encode('\r\n'),
      encoder.encode(`--${boundary}\r\nContent-Disposition: form-data; name="expected_version"\r\n\r\n${target.version}\r\n`),
      encoder.encode(`--${boundary}--\r\n`),
    ];
    const length = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
    const body = new Uint8Array(length);
    let offset = 0;
    for (const chunk of chunks) {
      body.set(chunk, offset);
      offset += chunk.length;
    }
    const response = await fetch(resolveUrl(`/v1/documents/${target.id}/versions`), {
      method: 'POST',
      headers: {
        Authorization: token,
        'Content-Type': `multipart/form-data; boundary=${boundary}`,
        'Idempotency-Key': 'idem-new-version-1',
      },
      body,
    });
    expect(response.status).toBe(200);
    const created = (await response.json()) as { document_id: string; document_version_id: string; job_id: string; version: number };
    expect(created.document_id).toBe(target.id);
    expect(created.version).toBe(target.version + 1);
    expect(created.job_id).toBeTruthy();

    // 任务 succeeded 前：旧 active 继续服务（active_version_id 未变）
    const pending = (await (await fetch(resolveUrl(`/v1/documents/${target.id}/versions`), { headers: { Authorization: token } })).json()) as {
      active_version_id: string | null;
      items: { document_version_id: string; status: string }[];
    };
    expect(pending.active_version_id).toBe(target.document_version_id);
    expect(pending.items.find((item) => item.document_version_id === created.document_version_id)?.status).toBe('processing');

    // 任务 succeeded：新版本切 active，旧版本转 superseded
    mockKnowledge.advanceJob(token, created.job_id, 'succeeded');
    const after = (await (await fetch(resolveUrl(`/v1/documents/${target.id}/versions`), { headers: { Authorization: token } })).json()) as {
      active_version_id: string | null;
      items: { document_version_id: string; status: string }[];
    };
    expect(after.active_version_id).toBe(created.document_version_id);
    expect(after.items.find((item) => item.document_version_id === target.document_version_id)?.status).toBe('superseded');
  });

  it('同 Idempotency-Key 同 payload 回放同一结果；不同 payload 409 idempotency_key_conflict', async () => {
    const token = bearerOf('zhangsan');
    const docs = (await (await listDocuments(token, 'personal:u_user')).json()) as {
      items: { id: string; version: number }[];
    };
    const target = docs.items[0];
    const file = { name: '重试文档.pdf', size: 4, type: 'application/pdf' };

    const first = mockKnowledge.uploadNewVersion(token, target.id, file, target.version, 'idem-retry-1');
    const second = mockKnowledge.uploadNewVersion(token, target.id, file, target.version, 'idem-retry-1');
    expect(second.job_id).toBe(first.job_id);
    expect(second.document_version_id).toBe(first.document_version_id);

    // 同键不同 payload → 409
    expectHttpError(
      () => mockKnowledge.uploadNewVersion(token, target.id, { name: '另一个文件.pdf', size: 5, type: 'application/pdf' }, target.version, 'idem-retry-1'),
      409,
      'idempotency_key_conflict',
    );
  });

  it('权限：他人个人库 / 不可写空间的上传新版本与文档操作 403/404', async () => {
    const zhangsanToken = bearerOf('zhangsan');
    const ministerToken = bearerOf('minister-li');
    // zhangsan 文档（个人库）：minister 不可上传新版本（他人个人库 read）
    const docs = (await (await listDocuments(zhangsanToken, 'personal:u_user')).json()) as {
      items: { id: string; version: number }[];
    };
    const target = docs.items[0];
    expectHttpError(
      () =>
        mockKnowledge.uploadNewVersion(
          ministerToken,
          target.id,
          { name: '越权.pdf', size: 4, type: 'application/pdf' },
          target.version,
          'idem-minister-cross',
        ),
      403,
      'space_upload_forbidden',
    );
    // 跨用户只读可见（minister 对他人个人库 read）但写操作 403；zhangsan 对部长个人库 read 同样只读
    const ministerDocs = (await (await listDocuments(ministerToken, 'personal:u_minister')).json()) as {
      items: { id: string }[];
    };
    expect(ministerDocs.items.length).toBeGreaterThan(0);
    expectHttpError(
      () =>
        mockKnowledge.uploadNewVersion(
          zhangsanToken,
          ministerDocs.items[0]!.id,
          { name: '越权2.pdf', size: 4, type: 'application/pdf' },
          1,
          'idem-zhangsan-cross',
        ),
      403,
      'space_upload_forbidden',
    );
  });

  it('初始上传：job 成功前文档不出现在列表，成功后出现', async () => {
    const token = bearerOf('zhangsan');
    const upload = await uploadRequest(
      token,
      'personal:u_user',
      [{ name: '生命周期文档.pdf', type: 'application/pdf', content: '%PDF' }],
      'idem-lifecycle-1',
    );
    const body = (await upload.json()) as { items: { job_id: string; name: string }[] };
    const jobId = body.items[0]?.job_id ?? '';
    expect(jobId).toBeTruthy();

    // 成功前不可见
    const before = (await (await listDocuments(token, 'personal:u_user')).json()) as { items: { name: string }[] };
    expect(before.items.some((item) => item.name === '生命周期文档.pdf')).toBe(false);

    mockKnowledge.advanceJob(token, jobId, 'succeeded');
    const after = (await (await listDocuments(token, 'personal:u_user')).json()) as { items: { name: string }[] };
    expect(after.items.some((item) => item.name === '生命周期文档.pdf')).toBe(true);
  });
});

describe('knowledge contract mock：审批文档生命周期与通知接收者（review C11）', () => {
  it('审核通过的文档 job 成功前不可检索，成功后激活；通知发给投稿人而非部长', async () => {
    const ministerToken = bearerOf('minister-li');
    const submitterToken = bearerOf('zhangsan');
    const list = (await (await fetch(resolveUrl('/v1/approvals/submissions'), { headers: { Authorization: ministerToken } })).json()) as {
      items: { submission_id: string; version: number; name: string }[];
    };
    const target = list.items[0];

    const approve = await fetch(resolveUrl(`/v1/approvals/submissions/${target.submission_id}/approve`), {
      method: 'POST',
      headers: { Authorization: ministerToken, 'Content-Type': 'application/json', 'Idempotency-Key': 'idem-approve-lifecycle' },
      body: JSON.stringify({ expected_version: target.version }),
    });
    expect(approve.status).toBe(200);
    const approved = (await approve.json()) as { job_id?: string; document_id?: string };

    // 通知发给投稿人（zhangsan）而非部长
    expect(mockNotifications.unreadCount(submitterToken)).toBeGreaterThan(0);

    // job 成功前：文档不可检索（部门库文档列表不出现）
    const before = (await (await listDocuments(ministerToken, 'department:d_finance')).json()) as { items: { name: string }[] };
    expect(before.items.some((item) => item.name === target.name)).toBe(false);

    // job 成功后：文档可检索
    const jobId = approved.job_id ?? '';
    expect(jobId).toBeTruthy();
    mockKnowledge.advanceJob(ministerToken, jobId, 'succeeded');
    const after = (await (await listDocuments(ministerToken, 'department:d_finance')).json()) as { items: { name: string }[] };
    expect(after.items.some((item) => item.name === target.name)).toBe(true);
  });

  it('幂等记录按 actor+endpoint+target 隔离：同 key 不同用户各自独立', async () => {
    const zhangsanToken = bearerOf('zhangsan');
    const opsToken = bearerOf('ops-wang');
    // zhangsan 上传一个新文档（同 key 不同 actor）
    const first = mockKnowledge.uploadDocuments(zhangsanToken, 'personal:u_user', [
      { name: '隔离文档.pdf', size: 4, type: 'application/pdf', contentHash: 'hash-aaa' },
    ], 'idem-shared-key');
    expect(first.items[0]?.accepted).toBe(true);

    // ops 用同 key 上传到自己的个人库：独立记录，不冲突
    const second = mockKnowledge.uploadDocuments(opsToken, 'personal:u_ops', [
      { name: '隔离文档2.pdf', size: 5, type: 'application/pdf', contentHash: 'hash-bbb' },
    ], 'idem-shared-key');
    expect(second.items[0]?.accepted).toBe(true);
    expect(second.items[0] && 'job_id' in second.items[0] ? (second.items[0].job_id ?? '') : '').not.toBe(
      first.items[0] && 'job_id' in first.items[0] ? (first.items[0].job_id ?? '') : '',
    );
  });
});
