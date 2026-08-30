/*
 * 管理面板契约 mock 传输层（MSW 接线；契约 §6.12、§8.1–8.3、§9.1–9.2、§10.1–10.2、
 * §11.1–11.3、§12.1–12.7）。与控制器分离：本文件只做 URL/参数/请求体解析与错误归一化。
 * 注册顺序：必须先于 knowledge handlers——GET /v1/approvals/summary 在此遮蔽同名路由
 * （ops 的 quota_pending 由 admin 域计数，投稿计数仍委托 knowledge 角色范围）。
 * 不在此注册（knowledge handlers 已服务，复用面）：
 * - /v1/approvals/submissions*（§8.4–8.5 投稿审核）与 /v1/submissions/:id/content；
 * - /v1/ingestion-jobs/:id/cancel|replay（§6.7 任务行操作）；
 * - spaces/documents 路由族（文档列表 / 版本 / 删除 / 重建 / 上传新版本）。
 */

import { http, HttpResponse } from 'msw';
import type {
  BackupPolicyPatchInput,
  CalibrationWindowAction,
  CalibrationWindowKind,
  DepartmentStatusFilter,
  MetricsWindow,
  OpsJobsView,
  QuotaRequestStatus,
} from '../admin/types';
import type { Role } from '../auth/types';
import { MockHttpError } from './auth-contract';
import type { MockAdminController } from './admin-contract';
import type { MockKnowledgeController } from './knowledge-contract';

const METRICS_WINDOWS: readonly MetricsWindow[] = ['today', '7d', '30d'];
const QUOTA_REQUEST_STATUSES: readonly QuotaRequestStatus[] = [
  'pending',
  'approved',
  'rejected',
  'cancelled',
];
const OPS_JOBS_VIEWS: readonly OpsJobsView[] = ['all', 'active', 'replayable', 'stale'];
const CALIBRATION_ACTIONS: readonly CalibrationWindowAction[] = ['open', 'close'];
const CALIBRATION_KINDS: readonly CalibrationWindowKind[] = ['cold_start', 'sentinel', 'manual'];
const ROLES: readonly Role[] = ['user', 'minister', 'ops', 'admin'];
const DEPARTMENT_STATUSES: readonly DepartmentStatusFilter[] = ['active', 'inactive', 'all'];

let requestSeq = 0;

function errorResponse(error: unknown) {
  const normalized = error instanceof MockHttpError ? error : new MockHttpError(500, 'internal_error');
  requestSeq += 1;
  return HttpResponse.json(
    {
      error: {
        code: normalized.code,
        message: normalized.code,
        details: normalized.details,
        request_id: `req_mock_admin_${requestSeq}`,
      },
    },
    { status: normalized.status },
  );
}

async function jsonObject(request: Request): Promise<Record<string, unknown>> {
  const body = await request.json().catch(() => null);
  if (body === null || typeof body !== 'object' || Array.isArray(body)) {
    throw new MockHttpError(422, 'validation_error');
  }
  return body as Record<string, unknown>;
}

function parseIntParam(value: string | null, fallback: number, field: string): number {
  if (value === null) {
    return fallback;
  }
  const parsed = Number(value);
  if (!Number.isInteger(parsed)) {
    throw new MockHttpError(422, 'validation_error', { field });
  }
  return parsed;
}

function parseEnumParam<T extends string>(
  value: string | null,
  allowed: readonly T[],
  fallback: T,
  field: string,
): T {
  if (value === null) {
    return fallback;
  }
  if (!allowed.includes(value as T)) {
    throw new MockHttpError(422, 'validation_error', { field });
  }
  return value as T;
}

/** 可选枚举查询参数：缺省/空串 → undefined；非法值 422。 */
function parseOptionalEnumParam<T extends string>(
  value: string | null,
  allowed: readonly T[],
  field: string,
): T | undefined {
  if (value === null || value === '') {
    return undefined;
  }
  if (!allowed.includes(value as T)) {
    throw new MockHttpError(422, 'validation_error', { field });
  }
  return value as T;
}

function requireIdempotencyKey(request: Request): string {
  const key = request.headers.get('Idempotency-Key');
  if (key === null || key.trim() === '') {
    throw new MockHttpError(422, 'validation_error', { field: 'idempotency_key' });
  }
  return key;
}

function requireExactKeys(body: Record<string, unknown>, keys: readonly string[]): void {
  const actual = Object.keys(body);
  if (actual.length !== keys.length || !keys.every((key) => Object.hasOwn(body, key))) {
    throw new MockHttpError(422, 'validation_error');
  }
}

/** 键子集 + 必填键：实际键 ⊆ allowed 且 required 全部出现。 */
function requireKeyShape(
  body: Record<string, unknown>,
  allowed: readonly string[],
  required: readonly string[],
): void {
  const actual = Object.keys(body);
  if (
    !actual.every((key) => allowed.includes(key)) ||
    !required.every((key) => Object.hasOwn(body, key))
  ) {
    throw new MockHttpError(422, 'validation_error');
  }
}

function requireExpectedVersion(body: Record<string, unknown>): number {
  const value = body['expected_version'];
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 1) {
    throw new MockHttpError(422, 'validation_error', { field: 'expected_version' });
  }
  return value;
}

function requireStringField(body: Record<string, unknown>, key: string): string {
  const value = body[key];
  if (typeof value !== 'string' || value.trim() === '') {
    throw new MockHttpError(422, 'validation_error', { field: key });
  }
  return value;
}

function optionalStringField(body: Record<string, unknown>, key: string): string | undefined {
  if (!Object.hasOwn(body, key)) {
    return undefined;
  }
  return requireStringField(body, key);
}

/** string | null 字段（如 department_id）：null 与缺省由调用方区分语义。 */
function requireStringOrNullField(body: Record<string, unknown>, key: string): string | null {
  const value = body[key];
  if (value === null) {
    return null;
  }
  return requireStringField(body, key);
}

function parseEnumField<T extends string>(
  value: unknown,
  allowed: readonly T[],
  field: string,
): T {
  if (typeof value !== 'string' || !allowed.includes(value as T)) {
    throw new MockHttpError(422, 'validation_error', { field });
  }
  return value as T;
}

function optionalIntField(body: Record<string, unknown>, key: string): number | undefined {
  if (!Object.hasOwn(body, key)) {
    return undefined;
  }
  const value = body[key];
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 1) {
    throw new MockHttpError(422, 'validation_error', { field: key });
  }
  return value;
}

function optionalQuery(value: string | null): string | undefined {
  return value === null || value === '' ? undefined : value;
}

/** HH:MM 本地时间（小时 00–23 / 分钟 00–59）。 */
function parseLocalTimeField(value: unknown): string {
  if (typeof value !== 'string' || !/^([01]\d|2[0-3]):[0-5]\d$/.test(value)) {
    throw new MockHttpError(422, 'validation_error', { field: 'local_time' });
  }
  return value;
}

/** 星期数组（0=周一 … 6=周日，与后端 Python date.weekday() 一致）；空数组合法与否由控制器按 frequency 判定。 */
function parseWeekdaysField(value: unknown): number[] {
  if (
    !Array.isArray(value) ||
    value.some((day) => !Number.isInteger(day) || (day as number) < 0 || (day as number) > 6)
  ) {
    throw new MockHttpError(422, 'validation_error', { field: 'weekdays' });
  }
  return value as number[];
}

/** IANA 时区：经 Intl 构造校验（无效名抛 RangeError）。 */
function parseTimezoneField(value: unknown): string {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new MockHttpError(422, 'validation_error', { field: 'timezone' });
  }
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: value });
  } catch {
    throw new MockHttpError(422, 'validation_error', { field: 'timezone' });
  }
  return value;
}

/** 策略 PATCH 请求体解析：expected_version 必填 + 字段子集（各字段独立校验）。 */
function parsePolicyPatch(body: Record<string, unknown>): BackupPolicyPatchInput {
  requireKeyShape(
    body,
    [
      'expected_version',
      'enabled',
      'frequency',
      'local_time',
      'weekdays',
      'timezone',
      'keep_last',
      'retention_days',
    ],
    ['expected_version'],
  );
  const input: {
    -readonly [K in keyof BackupPolicyPatchInput]?: BackupPolicyPatchInput[K];
  } = { expected_version: requireExpectedVersion(body) };
  if (Object.hasOwn(body, 'enabled')) {
    if (typeof body['enabled'] !== 'boolean') {
      throw new MockHttpError(422, 'validation_error', { field: 'enabled' });
    }
    input.enabled = body['enabled'];
  }
  if (Object.hasOwn(body, 'frequency')) {
    input.frequency = parseEnumField(body['frequency'], ['daily', 'weekly'] as const, 'frequency');
  }
  if (Object.hasOwn(body, 'local_time')) {
    input.local_time = parseLocalTimeField(body['local_time']);
  }
  if (Object.hasOwn(body, 'weekdays')) {
    input.weekdays = parseWeekdaysField(body['weekdays']);
  }
  if (Object.hasOwn(body, 'timezone')) {
    input.timezone = parseTimezoneField(body['timezone']);
  }
  const keepLast = optionalIntField(body, 'keep_last');
  if (keepLast !== undefined) {
    input.keep_last = keepLast;
  }
  const retentionDays = optionalIntField(body, 'retention_days');
  if (retentionDays !== undefined) {
    input.retention_days = retentionDays;
  }
  return input as BackupPolicyPatchInput;
}

export function createAdminHandlers(
  controller: MockAdminController,
  knowledge: MockKnowledgeController,
) {
  return [
    /* ---------- §9 指标 ---------- */

    http.get('/v1/metrics/dashboard', ({ request }) => {
      try {
        const url = new URL(request.url);
        const window = parseEnumParam(url.searchParams.get('window'), METRICS_WINDOWS, '7d', 'window');
        const rawExpand = url.searchParams.get('expand');
        if (rawExpand !== null && rawExpand !== '' && rawExpand !== 'user_rank') {
          throw new MockHttpError(422, 'validation_error', { field: 'expand' });
        }
        return HttpResponse.json(
          controller.getDashboard(request.headers.get('Authorization'), window, rawExpand as 'user_rank' | null),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/admin/users/:userId/documents', ({ request, params }) => {
      try {
        const url = new URL(request.url);
        const page = parseIntParam(url.searchParams.get('page'), 1, 'page');
        const pageSize = parseIntParam(url.searchParams.get('page_size'), 50, 'page_size');
        return HttpResponse.json(
          knowledge.listDocuments(
            request.headers.get('Authorization'),
            `personal:${String(params['userId'])}`,
            url.searchParams.get('q') ?? undefined,
            page,
            pageSize,
          ),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/admin/departments/:departmentId/documents', ({ request, params }) => {
      try {
        const url = new URL(request.url);
        const page = parseIntParam(url.searchParams.get('page'), 1, 'page');
        const pageSize = parseIntParam(url.searchParams.get('page_size'), 50, 'page_size');
        return HttpResponse.json(
          knowledge.listDocuments(
            request.headers.get('Authorization'),
            `department:${String(params['departmentId'])}`,
            url.searchParams.get('q') ?? undefined,
            page,
            pageSize,
          ),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/metrics/operations', ({ request }) => {
      try {
        const url = new URL(request.url);
        const window = parseEnumParam(url.searchParams.get('window'), METRICS_WINDOWS, '7d', 'window');
        return HttpResponse.json(
          controller.getOperationsMetrics(request.headers.get('Authorization'), window),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- §8.1–8.3 审批计数与配额申请审批 ---------- */

    // 遮蔽 knowledge 同名路由：ops 的 quota_pending 由 admin 域计数。
    http.get('/v1/approvals/summary', ({ request }) => {
      try {
        return HttpResponse.json(controller.getApprovalSummary(request.headers.get('Authorization')));
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/approvals/quota-requests', ({ request }) => {
      try {
        const url = new URL(request.url);
        const status = parseEnumParam(
          url.searchParams.get('status'),
          QUOTA_REQUEST_STATUSES,
          'pending',
          'status',
        );
        return HttpResponse.json(controller.listQuotaRequests(request.headers.get('Authorization'), status));
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/approvals/quota-requests/:requestId/approve', async ({ request, params }) => {
      try {
        const body = await jsonObject(request);
        requireKeyShape(body, ['expected_version', 'approved_pages'], ['expected_version']);
        const expectedVersion = requireExpectedVersion(body);
        const approvedPages = optionalIntField(body, 'approved_pages') ?? null;
        const idempotencyKey = requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.approveQuotaRequest(
            request.headers.get('Authorization'),
            String(params['requestId']),
            expectedVersion,
            approvedPages,
            idempotencyKey,
          ),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/approvals/quota-requests/:requestId/reject', async ({ request, params }) => {
      try {
        const body = await jsonObject(request);
        requireExactKeys(body, ['expected_version']);
        const expectedVersion = requireExpectedVersion(body);
        const idempotencyKey = requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.rejectQuotaRequest(
            request.headers.get('Authorization'),
            String(params['requestId']),
            expectedVersion,
            idempotencyKey,
          ),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- §6.12 公共库图谱维护（仅 ops） ---------- */

    http.get('/v1/ops/graph-builds/current', ({ request }) => {
      try {
        return HttpResponse.json(controller.getCurrentGraphBuild(request.headers.get('Authorization')));
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/ops/graph-builds', async ({ request }) => {
      try {
        const body = await jsonObject(request);
        requireExactKeys(body, ['expected_source_revision']);
        const revision = body['expected_source_revision'];
        if (typeof revision !== 'number' || !Number.isInteger(revision) || revision < 0) {
          throw new MockHttpError(422, 'validation_error', { field: 'expected_source_revision' });
        }
        const idempotencyKey = requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.createGraphBuild(request.headers.get('Authorization'), revision, idempotencyKey),
          { status: 202 },
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/ops/graph-builds/:graphBuildId/cancel', async ({ request, params }) => {
      try {
        const body = await jsonObject(request);
        requireExactKeys(body, ['expected_version']);
        const expectedVersion = requireExpectedVersion(body);
        const idempotencyKey = requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.cancelGraphBuild(
            request.headers.get('Authorization'),
            String(params['graphBuildId']),
            expectedVersion,
            idempotencyKey,
          ),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- §10 任务队列（读模型在 knowledge 域，行操作复用 knowledge 路由） ---------- */

    http.get('/v1/ops/jobs', ({ request }) => {
      try {
        const url = new URL(request.url);
        const view = parseEnumParam(url.searchParams.get('view'), OPS_JOBS_VIEWS, 'all', 'view');
        return HttpResponse.json(knowledge.listOpsJobs(request.headers.get('Authorization'), view));
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- 备份与恢复（backup-restore-operations-layer 规格 §2；严格 ops-only） ---------- */

    http.get('/v1/ops/backups', ({ request }) => {
      try {
        const url = new URL(request.url);
        const page = parseIntParam(url.searchParams.get('page'), 1, 'page');
        const pageSize = parseIntParam(url.searchParams.get('page_size'), 10, 'page_size');
        return HttpResponse.json(
          controller.listOpsBackups(request.headers.get('Authorization'), page, pageSize),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/ops/backups', ({ request }) => {
      try {
        // 无请求体；Idempotency-Key 必填
        const idempotencyKey = requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.createOpsBackup(request.headers.get('Authorization'), idempotencyKey),
          { status: 202 },
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/ops/backups/:backupId', ({ request, params }) => {
      try {
        return HttpResponse.json(
          controller.getOpsBackup(request.headers.get('Authorization'), String(params['backupId'])),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/ops/restores', ({ request }) => {
      try {
        const url = new URL(request.url);
        const page = parseIntParam(url.searchParams.get('page'), 1, 'page');
        const pageSize = parseIntParam(url.searchParams.get('page_size'), 10, 'page_size');
        return HttpResponse.json(
          controller.listOpsRestores(request.headers.get('Authorization'), page, pageSize),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/ops/restores', async ({ request }) => {
      try {
        const body = await jsonObject(request);
        requireExactKeys(body, ['backup_id']);
        const backupId = requireStringField(body, 'backup_id');
        const idempotencyKey = requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.createOpsRestore(
            request.headers.get('Authorization'),
            backupId,
            idempotencyKey,
          ),
          { status: 202 },
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/ops/restores/:restoreId', ({ request, params }) => {
      try {
        return HttpResponse.json(
          controller.getOpsRestore(request.headers.get('Authorization'), String(params['restoreId'])),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/ops/restores/:restoreId/repair-targets/:targetId/retry', async ({ request, params }) => {
      try {
        // 可无请求体；有则必须是空对象
        const body = await request.json().catch(() => null);
        if (body !== null && (typeof body !== 'object' || Array.isArray(body) || Object.keys(body).length > 0)) {
          throw new MockHttpError(422, 'validation_error');
        }
        const idempotencyKey = requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.retryOpsRepairTarget(
            request.headers.get('Authorization'),
            String(params['restoreId']),
            String(params['targetId']),
            idempotencyKey,
          ),
          { status: 202 },
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/ops/backup-policy', ({ request }) => {
      try {
        return HttpResponse.json(
          controller.getOpsBackupPolicy(request.headers.get('Authorization')),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.patch('/v1/ops/backup-policy', async ({ request }) => {
      try {
        const body = await jsonObject(request);
        const input = parsePolicyPatch(body);
        const idempotencyKey = requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.patchOpsBackupPolicy(
            request.headers.get('Authorization'),
            input,
            idempotencyKey,
          ),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- §11 评测与校准 ---------- */

    http.get('/v1/evaluation/leaderboard', ({ request }) => {
      try {
        return HttpResponse.json(controller.getLeaderboard(request.headers.get('Authorization')));
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.get('/v1/calibration/window', ({ request }) => {
      try {
        return HttpResponse.json(controller.getCalibrationWindow(request.headers.get('Authorization')));
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/calibration/window', async ({ request }) => {
      try {
        const body = await jsonObject(request);
        requireKeyShape(body, ['action', 'window_kind'], ['action']);
        const action = parseEnumField(body['action'], CALIBRATION_ACTIONS, 'action');
        const rawKind = body['window_kind'];
        const windowKind =
          rawKind === undefined || rawKind === null
            ? null
            : parseEnumField(rawKind, CALIBRATION_KINDS, 'window_kind');
        const idempotencyKey = requireIdempotencyKey(request);
        const result = controller.postCalibrationWindow(
          request.headers.get('Authorization'),
          action,
          windowKind,
          idempotencyKey,
        );
        return HttpResponse.json(result, { status: action === 'open' ? 201 : 200 });
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- §12.1–12.4 用户管理 ---------- */

    http.get('/v1/admin/users', ({ request }) => {
      try {
        const url = new URL(request.url);
        const role = parseOptionalEnumParam(url.searchParams.get('role'), ROLES, 'role');
        return HttpResponse.json(
          controller.listUsers(request.headers.get('Authorization'), {
            q: optionalQuery(url.searchParams.get('q')),
            departmentId: optionalQuery(url.searchParams.get('department_id')),
            role,
            page: parseIntParam(url.searchParams.get('page'), 1, 'page'),
            pageSize: parseIntParam(url.searchParams.get('page_size'), 20, 'page_size'),
          }),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/admin/users', async ({ request }) => {
      try {
        const body = await jsonObject(request);
        requireKeyShape(
          body,
          ['username', 'real_name', 'display_name', 'department_id', 'role', 'initial_password'],
          ['username', 'real_name', 'department_id', 'role', 'initial_password'],
        );
        // 与真实后端一致：用户写操作必须携带 Idempotency-Key（缺失 422）。
        requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.createUser(request.headers.get('Authorization'), {
            username: requireStringField(body, 'username'),
            real_name: requireStringField(body, 'real_name'),
            display_name: optionalStringField(body, 'display_name'),
            department_id: requireStringOrNullField(body, 'department_id'),
            role: parseEnumField(body['role'], ROLES, 'role'),
            initial_password: requireStringField(body, 'initial_password'),
          }),
          { status: 201 },
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.patch('/v1/admin/users/:userId', async ({ request, params }) => {
      try {
        const body = await jsonObject(request);
        requireKeyShape(body, ['expected_version', 'role', 'department_id'], ['expected_version']);
        requireIdempotencyKey(request);
        const expectedVersion = requireExpectedVersion(body);
        const input: { expected_version: number; role?: Role; department_id?: string | null } = {
          expected_version: expectedVersion,
        };
        if (Object.hasOwn(body, 'role')) {
          input.role = parseEnumField(body['role'], ROLES, 'role');
        }
        if (Object.hasOwn(body, 'department_id')) {
          input.department_id = requireStringOrNullField(body, 'department_id');
        }
        return HttpResponse.json(
          controller.patchUser(request.headers.get('Authorization'), String(params['userId']), input),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.delete('/v1/admin/users/:userId', async ({ request, params }) => {
      try {
        const body = await jsonObject(request);
        requireExactKeys(body, ['expected_version']);
        requireIdempotencyKey(request);
        const expectedVersion = requireExpectedVersion(body);
        return HttpResponse.json(
          controller.deleteUser(
            request.headers.get('Authorization'),
            String(params['userId']),
            expectedVersion,
          ),
          { status: 202 },
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- §12.5–12.6 部门目录与部门管理 ---------- */

    http.get('/v1/admin/departments', ({ request }) => {
      try {
        const url = new URL(request.url);
        const status = parseEnumParam(url.searchParams.get('status'), DEPARTMENT_STATUSES, 'active', 'status');
        return HttpResponse.json(controller.listDepartments(request.headers.get('Authorization'), status));
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/admin/departments', async ({ request }) => {
      try {
        const body = await jsonObject(request);
        requireExactKeys(body, ['name']);
        const idempotencyKey = requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.createDepartment(
            request.headers.get('Authorization'),
            requireStringField(body, 'name'),
            idempotencyKey,
          ),
          { status: 201 },
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.patch('/v1/admin/departments/:departmentId', async ({ request, params }) => {
      try {
        const body = await jsonObject(request);
        requireExactKeys(body, ['expected_version', 'name']);
        const expectedVersion = requireExpectedVersion(body);
        const idempotencyKey = requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.renameDepartment(
            request.headers.get('Authorization'),
            String(params['departmentId']),
            expectedVersion,
            requireStringField(body, 'name'),
            idempotencyKey,
          ),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    http.post('/v1/admin/departments/:departmentId/deactivate', async ({ request, params }) => {
      try {
        const body = await jsonObject(request);
        requireExactKeys(body, ['expected_version']);
        const expectedVersion = requireExpectedVersion(body);
        const idempotencyKey = requireIdempotencyKey(request);
        return HttpResponse.json(
          controller.deactivateDepartment(
            request.headers.get('Authorization'),
            String(params['departmentId']),
            expectedVersion,
            idempotencyKey,
          ),
        );
      } catch (error) {
        return errorResponse(error);
      }
    }),

    /* ---------- §12.7 权限矩阵（超管只读） ---------- */

    http.get('/v1/admin/permission-matrix', ({ request }) => {
      try {
        return HttpResponse.json(controller.getPermissionMatrix(request.headers.get('Authorization')));
      } catch (error) {
        return errorResponse(error);
      }
    }),
  ];
}
