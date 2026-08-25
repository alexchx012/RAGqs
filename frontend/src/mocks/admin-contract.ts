/*
 * 管理面板契约 mock 核心（契约《前端接口需求.md》§6.12、§8.1–8.3、§9.1–9.2、
 * §11.1–11.3、§12.1–12.7；后端设计 §2.6.1、§9.2）。
 * 与传输层无关（admin-handlers.ts 负责 MSW 接线）。真实模拟：
 * - dashboard 两种角色组包（ops 四包带 threshold/link、admin 四包恒 null），window 三档不同数据；
 * - 配额审批：version 校验 + 幂等作用域 + already_processed / not_approvable 系列 + 铃铛送达；
 * - 图谱状态机：disabled/ready/stale 投影 + run queued→running→succeeded 逐次 GET 推进、
 *   终态 graph_build_completed 送达发起者本人、错误码全集可达；
 * - 校准窗口三态与 409 系列；用户四角色目录与写操作错误系列；部门 allowed_actions 差异
 *   与停用阻断系列；权限矩阵静态只读。
 * 复用边界：投稿审核 / 任务队列 / 文档与空间实体归 MockKnowledgeController 所有（不另起炉灶），
 * 本控制器仅在其公共方法上组合 admin 视角；配额读数与申请归 MockQuotaStore。
 */

import type {
  AdminDepartmentItem,
  AdminUserItem,
  BackupPolicy,
  BackupPolicyPatchInput,
  CalibrationWindow,
  CalibrationWindowAction,
  CalibrationWindowKind,
  DashboardResponse,
  DepartmentStatusFilter,
  GraphBuildCancelResponse,
  GraphBuildCurrent,
  GraphBuildRun,
  GraphBuildStatus,
  LeaderboardResponse,
  MetricsWindow,
  OperationsMetricsResponse,
  OpsBackupCreateResponse,
  OpsBackupDetail,
  OpsBackupItem,
  OpsBackupListResponse,
  OpsRepairTargetRetryResponse,
  OpsRestoreCreateResponse,
  OpsRestoreDetail,
  OpsRestoreItem,
  OpsRestoreListResponse,
  PermissionMatrixResponse,
  QuotaApproveResponse,
  QuotaRejectResponse,
  QuotaRequestItem,
  QuotaRequestStatus,
  UserLifecycleStatus,
} from '../admin/types';
import type { Role, User } from '../auth/types';
import { copy } from '../copy';
import { MockHttpError } from './auth-contract';
import type { MockKnowledgeController } from './knowledge-contract';
import type { MockNotificationsController } from './notifications-contract';
import type { MockQuotaStore } from './quota-contract';

export type AdminAuth = (auth: string | null) => User;

const GRAPH_SPACE_ID = 'public' as const;
const PURGE_RETENTION_MS = 30 * 24 * 3600 * 1000;

const ROLE_LABELS: Record<Role, string> = {
  user: '普通用户',
  minister: '部长',
  ops: '运维',
  admin: '管理员',
};

/* ---------- 存储记录 ---------- */

interface StoredQuotaApproval {
  readonly id: string;
  version: number;
  status: QuotaRequestStatus;
  readonly applicantId: string;
  readonly applicantName: string;
  readonly used: number;
  readonly effectiveLimit: number;
  readonly requestedPages: number;
  approvedPages: number | null;
  readonly quotaPeriod: string;
  readonly createdAt: string;
  reviewedAt: string | null;
}

interface StoredGraphRun {
  readonly graphBuildId: string;
  version: number;
  status: GraphBuildStatus;
  readonly sourceRevision: number;
  readonly estimatedPrimaryModelCalls: number;
  actualUsage: { primary_model_calls: number; provider_calls: number } | null;
  readonly createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  failureClass: string | null;
  readonly initiatorId: string;
  /** 终态通知只写一次（租约恢复不产生第二条）。 */
  notified: boolean;
}

interface StoredCalibrationWindow {
  readonly windowId: string;
  status: 'open' | 'closing' | 'closed';
  readonly kind: CalibrationWindowKind;
  readonly policyVersion: string;
  readonly sampleRate: number;
  pairsCollected: number;
  readonly openedAt: string;
  closedAt: string | null;
  closeDeadlineAt: string | null;
  readonly openedBy: string | null;
  closedBy: string | null;
}

interface StoredAdminUser {
  readonly id: string;
  readonly username: string;
  readonly realName: string;
  displayName: string;
  department: { id: string; name: string } | null;
  role: Role;
  lastActiveAt: string | null;
  readonly documentCount: number;
  version: number;
  lifecycle: UserLifecycleStatus;
  deletionRequestedAt: string | null;
  purgeAfterAt: string | null;
}

interface StoredDepartment {
  readonly id: string;
  name: string;
  status: 'active' | 'inactive';
  version: number;
  readonly documentCount: number;
  readonly memberCount: number;
  readonly nonterminalJobCount: number;
  readonly pendingSubmissionCount: number;
  deactivatedAt: string | null;
}

interface StoredGraphProjection {
  sourceRevision: number;
  availability: 'disabled' | 'ready' | 'stale';
  activeGeneration: { graph_generation_id: string; source_revision: number; built_at: string } | null;
  latestRun: StoredGraphRun | null;
}

/* ---------- 备份与恢复存储记录（backup-restore-operations-layer） ---------- */

interface StoredBackupComponent {
  readonly kind: string;
  status: 'creating' | 'complete' | 'failed';
  reference: string | null;
  failureReason: string | null;
}

interface StoredBackup {
  readonly backupId: string;
  status: 'creating' | 'complete' | 'failed';
  readonly createdAt: string;
  completedAt: string | null;
  restorable: boolean;
  readonly components: StoredBackupComponent[];
  readonly initiatorId: string;
}

interface StoredRestoreStage {
  readonly stage: string;
  status: 'pending' | 'running' | 'succeeded' | 'failed';
}

interface StoredRepairTarget {
  readonly targetId: string;
  readonly stage: string;
  readonly resourceId: string;
  status: 'open' | 'succeeded';
  readonly failureClassification: string;
  attempts: number;
}

interface StoredRestore {
  readonly restoreId: string;
  readonly backupId: string;
  status: 'accepted' | 'running' | 'blocked' | 'succeeded' | 'failed';
  readonly createdAt: string;
  completedAt: string | null;
  failureReason: string | null;
  readonly stages: StoredRestoreStage[];
  readonly repairTargets: StoredRepairTarget[];
}

interface StoredBackupPolicy {
  enabled: boolean;
  frequency: 'daily' | 'weekly';
  localTime: string;
  weekdays: number[];
  timezone: string;
  keepLast: number;
  retentionDays: number;
  version: number;
  lastScheduledFor: string | null;
  lastOutcome: string | null;
}

/** 固定恢复阶段（与内部状态机同一词表与顺序）。 */
const OPS_RESTORE_STAGES: readonly string[] = [
  'postgres',
  'object_store',
  'milvus',
  'sparse',
  'summary',
  'graph',
  'cache',
];

const OPS_BACKUP_COMPONENT_KINDS: readonly string[] = [
  'postgres_snapshot',
  'object_store_snapshot',
  'object_manifest',
];

/** mock 下一次执行窗口：UTC 当日 local_time，已过则顺延一日（不含真实时区换算）。 */
function nextPolicyRunAt(localTime: string): string {
  const now = new Date();
  const [hours, minutes] = localTime.split(':').map((part) => Number(part));
  const next = new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), hours ?? 0, minutes ?? 0),
  );
  if (next.getTime() <= now.getTime()) {
    next.setUTCDate(next.getUTCDate() + 1);
  }
  return next.toISOString();
}

const EVALUATION_POLICY = {
  policy_version: 'eval_2026_v1',
  min_real_queries: 50,
  shadow_max_examples: 200,
  shadow_max_candidate_configs: 3,
  calibration_open_score_gap: 0.03,
  cold_start_sample_rate: 0.4,
  sentinel_sample_rate: 0.03,
} as const;

export class MockAdminController {
  private readonly quotaApprovals = new Map<string, StoredQuotaApproval>();
  private graph: StoredGraphProjection = {
    sourceRevision: 12,
    availability: 'stale',
    activeGeneration: null,
    latestRun: null,
  };
  private calibrationWindow: StoredCalibrationWindow | null = null;
  private calibrationEligible = true;
  private graphEstimateAvailable = true;
  private graphNextOutcome: 'succeeded' | 'failed' | null = null;
  private readonly adminUsers = new Map<string, StoredAdminUser>();
  private readonly departments = new Map<string, StoredDepartment>();
  private readonly deactivationUnverified = new Set<string>();
  /** 备份与恢复：备份集 / 恢复会话 / 版本化策略（备份与恢复运维层）。 */
  private readonly backups = new Map<string, StoredBackup>();
  private readonly restores = new Map<string, StoredRestore>();
  private backupPolicy: StoredBackupPolicy = {
    enabled: true,
    frequency: 'daily',
    localTime: '02:30',
    weekdays: [0, 1, 2, 3, 4],
    timezone: 'Asia/Shanghai',
    keepLast: 7,
    retentionDays: 30,
    version: 1,
    lastScheduledFor: null,
    lastOutcome: null,
  };
  /** retry 已受理、待下一次状态读推进为 succeeded 的修复目标。 */
  private readonly pendingRepairResolution = new Set<string>();
  /** 写操作幂等回放：scopedKey → { payload, result }；同键同请求在状态校验前返回首次快照。 */
  private readonly idempotency = new Map<string, { payload: string; result: unknown }>();
  private seq = 0;

  constructor(
    private readonly auth: AdminAuth,
    private readonly knowledge: MockKnowledgeController,
    private readonly notifications: MockNotificationsController,
    private readonly quota: MockQuotaStore,
  ) {
    this.reset();
  }

  reset(): void {
    this.quotaApprovals.clear();
    this.adminUsers.clear();
    this.departments.clear();
    this.deactivationUnverified.clear();
    this.idempotency.clear();
    this.backups.clear();
    this.restores.clear();
    this.pendingRepairResolution.clear();
    this.calibrationWindow = null;
    this.calibrationEligible = true;
    this.graphEstimateAvailable = true;
    this.graphNextOutcome = null;
    this.seq = 0;
    this.seedFixtures();
  }

  /* ---------- 幂等回放（与 knowledge 域同一语义） ---------- */

  private idempotent<T>(
    key: string,
    payload: string,
    action: () => T,
    actorId: string,
    operation: string,
    target: string,
  ): T {
    if (key.trim() === '') {
      throw new MockHttpError(422, 'validation_error', { field: 'idempotency_key' });
    }
    const scopedKey = `${actorId}:${operation}:${target}:${key}`;
    const existing = this.idempotency.get(scopedKey);
    if (existing !== undefined) {
      if (existing.payload !== payload) {
        throw new MockHttpError(409, 'idempotency_key_conflict');
      }
      return structuredClone(existing.result) as T;
    }
    const result = action();
    this.idempotency.set(scopedKey, { payload, result });
    return structuredClone(result) as T;
  }

  /* ---------- 夹具 ---------- */

  /** 图谱投影夹具：切换 availability / source_revision（disabled/ready/stale 种子路径）。 */
  setGraphProjection(input: {
    readonly availability: 'disabled' | 'ready' | 'stale';
    readonly sourceRevision?: number;
    readonly activeGeneration?: { graph_generation_id: string; source_revision: number; built_at: string } | null;
  }): void {
    this.graph.availability = input.availability;
    if (input.sourceRevision !== undefined) {
      this.graph.sourceRevision = input.sourceRevision;
    }
    if (input.activeGeneration !== undefined) {
      this.graph.activeGeneration = input.activeGeneration;
    }
  }

  /** 图谱预估夹具：置 false 后 POST /ops/graph-builds 返回 503 graph_build_estimate_unavailable。 */
  setGraphEstimateAvailable(available: boolean): void {
    this.graphEstimateAvailable = available;
  }

  /** 图谱终态夹具：置 'failed' 后下一次 running→终态推进落入 failed（failure_class 固定样例）。 */
  setGraphNextOutcome(outcome: 'succeeded' | 'failed' | null): void {
    this.graphNextOutcome = outcome;
  }

  /** 图谱 run 显式推进（与 GET current 的逐次推进同逻辑；测试时钟友好）。 */
  advanceGraphRun(): void {
    const run = this.graph.latestRun;
    if (run === null || this.isGraphTerminal(run.status)) {
      return;
    }
    this.progressGraphRun(run);
  }

  /** 校准开窗资格夹具：置 false 后 cold_start/sentinel 开窗返回 409 calibration_window_not_eligible。 */
  setCalibrationEligible(eligible: boolean): void {
    this.calibrationEligible = eligible;
  }

  /** 校准窗口夹具：直接播种 open / closing 窗口（already_open / closing 错误路径）。 */
  seedCalibrationWindow(status: 'open' | 'closing', kind: CalibrationWindowKind = 'manual'): void {
    this.calibrationWindow = {
      windowId: this.nextId('cw'),
      status,
      kind,
      policyVersion: EVALUATION_POLICY.policy_version,
      sampleRate: this.sampleRateFor(kind),
      pairsCollected: status === 'closing' ? 132 : 12,
      openedAt: '2026-08-03T02:00:00Z',
      closedAt: null,
      closeDeadlineAt: status === 'closing' ? '2026-08-05T02:00:00Z' : null,
      openedBy: 'u_ops',
      closedBy: null,
    };
  }

  /** 收口完成夹具：closing → closed（读端点最终返回 closed）。 */
  completeCalibrationClosing(): void {
    const window = this.calibrationWindow;
    if (window === null || window.status !== 'closing') {
      return;
    }
    window.status = 'closed';
    window.closedAt = new Date().toISOString();
    window.closeDeadlineAt = null;
    window.closedBy = 'u_ops';
  }

  /** 部门停用依赖检查夹具：目标部门下次 deactivate 返回 503 department_deactivation_unverified。 */
  setDepartmentDeactivationUnverified(departmentId: string, unverified: boolean): void {
    if (unverified) {
      this.deactivationUnverified.add(departmentId);
    } else {
      this.deactivationUnverified.delete(departmentId);
    }
  }

  /* ---------- §9.1 dashboard（后端按角色组包） ---------- */

  getDashboard(
    auth: string | null,
    window: MetricsWindow,
    expand: 'user_rank' | null = null,
  ): DashboardResponse {
    const user = this.requireOpsOrAdmin(auth, 'metrics_forbidden');
    this.requireWindow(window);
    return user.role === 'ops'
      ? this.buildOpsDashboard(window)
      : this.buildAdminDashboard(window, expand === 'user_rank');
  }

  /* ---------- §9.2 指标看板（两端内容相同） ---------- */

  getOperationsMetrics(auth: string | null, window: MetricsWindow): OperationsMetricsResponse {
    this.requireOpsOrAdmin(auth, 'metrics_forbidden');
    this.requireWindow(window);
    const scale = windowScale(window);
    return {
      window,
      cards: [
        {
          key: 'cache_hit_rate',
          title: '缓存命中率',
          kind: 'stat',
          value: round2(0.68 + scale * 0.04),
          delta: { direction: 'up', text_hint: '+2.4%' },
          sparkline: scaledSeries([0.55, 0.6, 0.62, 0.66, 0.7], 1 + scale * 0.05),
          threshold: { value: 0.6, direction: 'below' },
          link: null,
        },
        {
          key: 'ocr_confidence_dist',
          title: 'OCR 置信度分布',
          kind: 'distribution',
          rows: [
            { label: '90–100%', value: Math.round(82 * scale), ratio: 0.82, tone: 'normal' },
            { label: '<90%', value: Math.round(18 * scale), ratio: 0.18, tone: 'warning' },
          ],
          threshold: null,
          link: null,
        },
        {
          key: 'graph_basic_split',
          title: '建树 / basic 分流比例',
          kind: 'distribution',
          rows: [
            { label: '建树', value: Math.round(35 * scale), ratio: 0.35, tone: 'normal' },
            { label: 'basic', value: Math.round(65 * scale), ratio: 0.65, tone: 'normal' },
          ],
          threshold: null,
          link: null,
        },
      ],
    };
  }

  /* ---------- §8.1 审批计数徽标（ops 配额计数在此；投稿计数委托 knowledge 角色范围） ---------- */

  getApprovalSummary(auth: string | null): { quota_pending: number; submission_pending: number } {
    const user = this.auth(auth);
    const submissionSide = this.knowledge.getApprovalSummary(auth);
    if (user.role !== 'ops') {
      return submissionSide;
    }
    const quotaPending = [...this.quotaApprovals.values()].filter(
      (request) => request.status === 'pending',
    ).length;
    return { quota_pending: quotaPending, submission_pending: submissionSide.submission_pending };
  }

  /* ---------- §8.2–8.3 配额申请审批（仅运维） ---------- */

  listQuotaRequests(auth: string | null, status: QuotaRequestStatus = 'pending'): { items: QuotaRequestItem[] } {
    const user = this.auth(auth);
    if (user.role !== 'ops') {
      throw new MockHttpError(403, 'approval_forbidden');
    }
    if (!['pending', 'approved', 'rejected', 'cancelled'].includes(status)) {
      throw new MockHttpError(422, 'validation_error', { field: 'status' });
    }
    const items = [...this.quotaApprovals.values()]
      .filter((request) => request.status === status)
      .sort((a, b) => a.createdAt.localeCompare(b.createdAt))
      .map((request) => this.toQuotaRequestItem(request));
    return { items };
  }

  approveQuotaRequest(
    auth: string | null,
    requestId: string,
    expectedVersion: number,
    approvedPages: number | null,
    idempotencyKey: string,
  ): QuotaApproveResponse {
    const user = this.auth(auth);
    if (user.role !== 'ops') {
      throw new MockHttpError(403, 'approval_forbidden');
    }
    const payload = JSON.stringify({ requestId, expectedVersion, approvedPages, action: 'approve' });
    return this.idempotent(
      idempotencyKey,
      payload,
      () => {
        const request = this.requireApprovableQuotaRequest(requestId, expectedVersion);
        const pages = approvedPages ?? request.requestedPages;
        if (!Number.isInteger(pages) || pages < 1 || pages > request.requestedPages) {
          throw new MockHttpError(422, 'validation_error', { field: 'approved_pages' });
        }
        request.version += 1;
        request.status = 'approved';
        request.approvedPages = pages;
        request.reviewedAt = new Date().toISOString();
        const creditEntryId = this.nextId('ce');
        this.resolveQuotaLinkage(request.applicantId, true);
        this.notifications.addNotification(request.applicantId, {
          type: 'quota_approved',
          title: '你的配额增加申请已通过',
          payload: { request_id: request.id },
        });
        return {
          id: request.id,
          version: request.version,
          status: 'approved' as const,
          approved_pages: pages,
          credit_entry_id: creditEntryId,
          quota_period: request.quotaPeriod,
        };
      },
      user.id,
      'quota-approve',
      requestId,
    );
  }

  rejectQuotaRequest(
    auth: string | null,
    requestId: string,
    expectedVersion: number,
    idempotencyKey: string,
  ): QuotaRejectResponse {
    const user = this.auth(auth);
    if (user.role !== 'ops') {
      throw new MockHttpError(403, 'approval_forbidden');
    }
    const payload = JSON.stringify({ requestId, expectedVersion, action: 'reject' });
    return this.idempotent(
      idempotencyKey,
      payload,
      () => {
        const request = this.requireApprovableQuotaRequest(requestId, expectedVersion);
        request.version += 1;
        request.status = 'rejected';
        request.reviewedAt = new Date().toISOString();
        this.resolveQuotaLinkage(request.applicantId, false);
        this.notifications.addNotification(request.applicantId, {
          type: 'quota_rejected',
          title: '你的配额增加申请已被驳回',
          payload: { request_id: request.id },
        });
        return { id: request.id, version: request.version, status: 'rejected' as const };
      },
      user.id,
      'quota-reject',
      requestId,
    );
  }

  private requireApprovableQuotaRequest(requestId: string, expectedVersion: number): StoredQuotaApproval {
    const request = this.quotaApprovals.get(requestId);
    if (request === undefined) {
      throw new MockHttpError(404, 'quota_request_not_found');
    }
    if (request.status !== 'pending') {
      throw new MockHttpError(409, 'already_processed');
    }
    if (request.version !== expectedVersion) {
      throw new MockHttpError(409, 'version_conflict');
    }
    // 申请人已冻结或目标月份已关闭：不可审批。
    const applicant = this.adminUsers.get(request.applicantId);
    if (applicant === undefined || applicant.lifecycle !== 'active') {
      throw new MockHttpError(409, 'quota_request_not_approvable');
    }
    return request;
  }

  /** 配额联动：申请人在共享配额库有待处理申请时按审批结果结算（次月语义由夹具显式触发）。 */
  private resolveQuotaLinkage(applicantId: string, approved: boolean): void {
    if (this.quota.snapshot(applicantId).pending_request !== null) {
      this.quota.resolvePending(applicantId, approved);
    }
  }

  /* ---------- §6.12 公共库图谱维护（仅 ops；后端设计 §2.6.1） ---------- */

  getCurrentGraphBuild(auth: string | null): GraphBuildCurrent {
    this.requireOps(auth);
    const run = this.graph.latestRun;
    // 轮询推进：每次状态查询把非终态 run 推进一拍（queued→running→终态）。
    if (run !== null && !this.isGraphTerminal(run.status)) {
      this.progressGraphRun(run);
    }
    return {
      space_id: GRAPH_SPACE_ID,
      source_revision: this.graph.sourceRevision,
      graph_availability: this.graph.availability,
      active_generation: this.graph.activeGeneration,
      latest_run: this.graph.latestRun === null ? null : this.toGraphRun(this.graph.latestRun),
    };
  }

  createGraphBuild(auth: string | null, expectedSourceRevision: number, idempotencyKey: string): GraphBuildRun {
    const user = this.requireOps(auth);
    const payload = JSON.stringify({ expectedSourceRevision });
    return this.idempotent(
      idempotencyKey,
      payload,
      () => {
        if (!this.graphEstimateAvailable) {
          throw new MockHttpError(503, 'graph_build_estimate_unavailable');
        }
        if (expectedSourceRevision !== this.graph.sourceRevision) {
          throw new MockHttpError(409, 'graph_source_changed');
        }
        if (this.graph.latestRun !== null && !this.isGraphTerminal(this.graph.latestRun.status)) {
          throw new MockHttpError(409, 'graph_build_in_progress');
        }
        const publicDocs = this.knowledge.listDocuments(auth, GRAPH_SPACE_ID, undefined, 1, 1).total;
        if (publicDocs === 0) {
          throw new MockHttpError(422, 'graph_source_empty');
        }
        const run: StoredGraphRun = {
          graphBuildId: this.nextId('gb'),
          version: 1,
          status: 'queued',
          sourceRevision: this.graph.sourceRevision,
          estimatedPrimaryModelCalls: publicDocs * 3,
          actualUsage: null,
          createdAt: new Date().toISOString(),
          startedAt: null,
          finishedAt: null,
          failureClass: null,
          initiatorId: user.id,
          notified: false,
        };
        this.graph.latestRun = run;
        return this.toGraphRun(run);
      },
      user.id,
      'graph-build-create',
      GRAPH_SPACE_ID,
    );
  }

  cancelGraphBuild(
    auth: string | null,
    graphBuildId: string,
    expectedVersion: number,
    idempotencyKey: string,
  ): GraphBuildCancelResponse {
    const user = this.requireOps(auth);
    const payload = JSON.stringify({ graphBuildId, expectedVersion });
    return this.idempotent(
      idempotencyKey,
      payload,
      () => {
        const run = this.graph.latestRun;
        if (run === null || run.graphBuildId !== graphBuildId) {
          throw new MockHttpError(404, 'graph_build_not_found');
        }
        // 已取消的 run 重复取消返回当前终态；其他终态 409。
        if (run.status === 'cancelled') {
          return this.toGraphCancelResponse(run);
        }
        if (this.isGraphTerminal(run.status)) {
          throw new MockHttpError(409, 'graph_build_not_cancellable');
        }
        if (run.version !== expectedVersion) {
          throw new MockHttpError(409, 'version_conflict');
        }
        run.status = 'cancelled';
        run.version += 1;
        run.finishedAt = new Date().toISOString();
        this.notifyGraphTerminal(run);
        return this.toGraphCancelResponse(run);
      },
      user.id,
      'graph-build-cancel',
      graphBuildId,
    );
  }

  private requireOps(auth: string | null): User {
    const user = this.auth(auth);
    if (user.role !== 'ops') {
      throw new MockHttpError(403, 'graph_build_forbidden');
    }
    return user;
  }

  private isGraphTerminal(status: GraphBuildStatus): boolean {
    return status === 'succeeded' || status === 'failed' || status === 'cancelled';
  }

  /** 非终态推进一拍：queued→running→succeeded（或夹具指定 failed）；每次转换 version +1。 */
  private progressGraphRun(run: StoredGraphRun): void {
    if (run.status === 'queued') {
      run.status = 'running';
      run.startedAt = new Date().toISOString();
      run.version += 1;
      return;
    }
    if (run.status !== 'running') {
      return;
    }
    const failed = this.graphNextOutcome === 'failed';
    this.graphNextOutcome = null;
    run.status = failed ? 'failed' : 'succeeded';
    run.finishedAt = new Date().toISOString();
    run.version += 1;
    if (failed) {
      run.failureClass = 'embedding_error';
      run.actualUsage = { primary_model_calls: 1, provider_calls: 1 };
    } else {
      run.actualUsage = {
        primary_model_calls: run.estimatedPrimaryModelCalls,
        provider_calls: run.estimatedPrimaryModelCalls + 2,
      };
      // worker 只可激活与当前 source_revision 相同的 staged generation。
      if (run.sourceRevision === this.graph.sourceRevision) {
        this.graph.activeGeneration = {
          graph_generation_id: this.nextId('gg'),
          source_revision: run.sourceRevision,
          built_at: run.finishedAt,
        };
        this.graph.availability = 'ready';
      } else {
        this.graph.availability = 'stale';
      }
    }
    this.notifyGraphTerminal(run);
  }

  /** 终态事务写唯一 graph_build_completed outbox 事件：接收者固定为发起者本人。 */
  private notifyGraphTerminal(run: StoredGraphRun): void {
    if (run.notified) {
      return;
    }
    run.notified = true;
    const title =
      run.status === 'succeeded'
        ? '公共库图谱构建已完成'
        : run.status === 'failed'
          ? '公共库图谱构建失败'
          : '公共库图谱构建已取消';
    this.notifications.addNotification(run.initiatorId, {
      type: 'graph_build_completed',
      title,
      payload: {
        graph_build_id: run.graphBuildId,
        status: run.status,
        source_revision: String(run.sourceRevision),
        ...(run.status === 'succeeded' && this.graph.activeGeneration !== null
          ? { graph_generation_id: this.graph.activeGeneration.graph_generation_id }
          : {}),
        ...(run.failureClass === null ? {} : { failure_class: run.failureClass }),
      },
    });
  }

  private toGraphRun(run: StoredGraphRun): GraphBuildRun {
    return {
      graph_build_id: run.graphBuildId,
      version: run.version,
      state: run.status,
      source_revision: run.sourceRevision,
      estimated_primary_model_calls: run.estimatedPrimaryModelCalls,
      actual_usage: run.actualUsage,
      created_at: run.createdAt,
      started_at: run.startedAt,
      completed_at: run.finishedAt,
      failure_class: run.failureClass,
      allowed_actions: run.status === 'queued' || run.status === 'running' ? ['cancel'] : [],
    };
  }

  private toGraphCancelResponse(run: StoredGraphRun): GraphBuildCancelResponse {
    return { graph_build_id: run.graphBuildId, version: run.version, state: run.status };
  }

  /* ---------- 备份与恢复（backup-restore-operations-layer 规格 §2/§3/§7；严格 ops-only） ---------- */

  private requireOpsBackups(auth: string | null): User {
    const user = this.auth(auth);
    if (user.role !== 'ops') {
      throw new MockHttpError(403, 'backup_forbidden');
    }
    return user;
  }

  /** active restore 互斥事实（accepted/running/blocked）；恢复期间维护门禁与本判定共用。 */
  private hasActiveRestore(): boolean {
    return [...this.restores.values()].some(
      (restore) =>
        restore.status === 'accepted' || restore.status === 'running' || restore.status === 'blocked',
    );
  }

  listOpsBackups(auth: string | null, page: number, pageSize: number): OpsBackupListResponse {
    this.requireOpsBackups(auth);
    // 创建时间倒序
    const all = [...this.backups.values()].sort((a, b) => b.createdAt.localeCompare(a.createdAt));
    const start = (page - 1) * pageSize;
    const items = all.slice(start, start + pageSize).map((backup) => this.toBackupItem(backup));
    // 读推进（先响应后推进）：creating 备份每次状态读推进一拍，首轮读仍呈现 creating
    for (const backup of all) {
      this.progressBackup(backup);
    }
    return { items, page, page_size: pageSize, total: all.length };
  }

  createOpsBackup(auth: string | null, idempotencyKey: string): OpsBackupCreateResponse {
    const user = this.requireOpsBackups(auth);
    return this.idempotent(
      idempotencyKey,
      'manual-backup',
      () => {
        // 恢复期间维护门禁：创建备份 503 maintenance_mode（规格 §7 白名单）
        if (this.hasActiveRestore()) {
          throw new MockHttpError(503, 'maintenance_mode');
        }
        const backup: StoredBackup = {
          backupId: this.nextId('bk'),
          status: 'creating',
          createdAt: new Date().toISOString(),
          completedAt: null,
          restorable: false,
          components: OPS_BACKUP_COMPONENT_KINDS.map((kind) => ({
            kind,
            status: 'creating' as const,
            reference: null,
            failureReason: null,
          })),
          initiatorId: user.id,
        };
        this.backups.set(backup.backupId, backup);
        return { backup_id: backup.backupId, status: backup.status };
      },
      user.id,
      'backup-create',
      'instance',
    );
  }

  getOpsBackup(auth: string | null, backupId: string): OpsBackupDetail {
    this.requireOpsBackups(auth);
    const backup = this.backups.get(backupId);
    if (backup === undefined) {
      throw new MockHttpError(404, 'backup_not_found');
    }
    const detail = this.toBackupDetail(backup);
    this.progressBackup(backup);
    return detail;
  }

  /** 非终态推进一拍：creating→complete（组成物同步完成并补齐 reference，备份转为可恢复）。 */
  private progressBackup(backup: StoredBackup): void {
    if (backup.status !== 'creating') {
      return;
    }
    backup.status = 'complete';
    backup.completedAt = new Date().toISOString();
    backup.restorable = true;
    for (const component of backup.components) {
      if (component.status === 'creating') {
        component.status = 'complete';
        component.reference = `${component.kind}:${backup.backupId}`;
      }
    }
  }

  listOpsRestores(auth: string | null, page: number, pageSize: number): OpsRestoreListResponse {
    this.requireOpsBackups(auth);
    const all = [...this.restores.values()].sort((a, b) => b.createdAt.localeCompare(a.createdAt));
    const start = (page - 1) * pageSize;
    const items = all.slice(start, start + pageSize).map((restore) => this.toRestoreItem(restore));
    // 读推进（先响应后推进）：与备份同一约定
    for (const restore of all) {
      this.progressRestore(restore);
    }
    return { items, page, page_size: pageSize, total: all.length };
  }

  createOpsRestore(
    auth: string | null,
    backupId: string,
    idempotencyKey: string,
  ): OpsRestoreCreateResponse {
    const user = this.requireOpsBackups(auth);
    const payload = JSON.stringify({ backupId });
    return this.idempotent(
      idempotencyKey,
      payload,
      () => {
        const backup = this.backups.get(backupId);
        if (backup === undefined) {
          throw new MockHttpError(404, 'backup_not_found');
        }
        if (!backup.restorable) {
          throw new MockHttpError(409, 'backup_not_restorable');
        }
        // 持久互斥：已有 active restore 时第二个恢复 409（规格 §7/Q9）
        if (this.hasActiveRestore()) {
          throw new MockHttpError(409, 'restore_in_progress');
        }
        const restore: StoredRestore = {
          restoreId: this.nextId('rs'),
          backupId,
          status: 'accepted',
          createdAt: new Date().toISOString(),
          completedAt: null,
          failureReason: null,
          stages: OPS_RESTORE_STAGES.map((stage) => ({ stage, status: 'pending' as const })),
          repairTargets: [],
        };
        this.restores.set(restore.restoreId, restore);
        return { restore_id: restore.restoreId, backup_id: backupId, status: restore.status };
      },
      user.id,
      'restore-create',
      backupId,
    );
  }

  getOpsRestore(auth: string | null, restoreId: string): OpsRestoreDetail {
    this.requireOpsBackups(auth);
    const restore = this.restores.get(restoreId);
    if (restore === undefined) {
      throw new MockHttpError(404, 'restore_not_found');
    }
    const detail = this.toRestoreDetail(restore);
    this.progressRestore(restore);
    return detail;
  }

  /**
   * 非终态推进一拍：accepted→running 并启动首阶段；running 推进一个阶段；
   * blocked 先结算已受理重试的修复目标（全部 succeeded 后解除阻断，重跑失败阶段）；
   * 全部阶段 succeeded 后转终态。
   */
  private progressRestore(restore: StoredRestore): void {
    if (restore.status === 'accepted') {
      restore.status = 'running';
      const first = restore.stages[0];
      if (first !== undefined) {
        first.status = 'running';
      }
      return;
    }
    if (restore.status === 'blocked') {
      for (const target of restore.repairTargets) {
        if (target.status === 'open' && this.pendingRepairResolution.has(target.targetId)) {
          target.status = 'succeeded';
          this.pendingRepairResolution.delete(target.targetId);
        }
      }
      if (restore.repairTargets.some((target) => target.status === 'open')) {
        return;
      }
      // mock 简化：阻断解除后一拍补齐剩余阶段并转终态（真实逐阶段推进由后端 worker 负责）
      for (const stage of restore.stages) {
        if (stage.status !== 'succeeded') {
          stage.status = 'succeeded';
        }
      }
      restore.status = 'succeeded';
      restore.completedAt = new Date().toISOString();
      return;
    }
    if (restore.status !== 'running') {
      return;
    }
    const running = restore.stages.find((stage) => stage.status === 'running');
    if (running !== undefined) {
      running.status = 'succeeded';
      const next = restore.stages.find((stage) => stage.status === 'pending');
      if (next !== undefined) {
        next.status = 'running';
        return;
      }
    }
    if (
      restore.stages.length > 0 &&
      restore.stages.every((stage) => stage.status === 'succeeded')
    ) {
      restore.status = 'succeeded';
      restore.completedAt = new Date().toISOString();
    }
  }

  retryOpsRepairTarget(
    auth: string | null,
    restoreId: string,
    targetId: string,
    idempotencyKey: string,
  ): OpsRepairTargetRetryResponse {
    const user = this.requireOpsBackups(auth);
    const payload = JSON.stringify({ restoreId, targetId });
    return this.idempotent(
      idempotencyKey,
      payload,
      () => {
        const restore = this.restores.get(restoreId);
        if (restore === undefined) {
          throw new MockHttpError(404, 'restore_not_found');
        }
        const target = restore.repairTargets.find((candidate) => candidate.targetId === targetId);
        if (target === undefined) {
          throw new MockHttpError(404, 'repair_target_not_found');
        }
        if (target.status !== 'open') {
          throw new MockHttpError(409, 'repair_target_not_open');
        }
        target.attempts += 1;
        // 重试受理后下一次状态读推进为 succeeded（持久 repair queue 语义）
        this.pendingRepairResolution.add(target.targetId);
        return { target_id: target.targetId, status: target.status };
      },
      user.id,
      'repair-retry',
      targetId,
    );
  }

  /**
   * 恢复终态夹具：把指定恢复直接推进到终态 succeeded（所有阶段与修复目标 succeeded），
   * 用于测试前清空 active 互斥，避免 createOpsRestore 409 / 维护门禁 503 干扰后续用例。
   */
  completeOpsRestore(restoreId: string): void {
    const restore = this.restores.get(restoreId);
    if (restore === undefined) {
      return;
    }
    for (const stage of restore.stages) {
      stage.status = 'succeeded';
    }
    for (const target of restore.repairTargets) {
      target.status = 'succeeded';
      this.pendingRepairResolution.delete(target.targetId);
    }
    restore.status = 'succeeded';
    restore.completedAt = new Date().toISOString();
  }

  getOpsBackupPolicy(auth: string | null): BackupPolicy {
    this.requireOpsBackups(auth);
    return this.toBackupPolicy();
  }

  patchOpsBackupPolicy(
    auth: string | null,
    input: BackupPolicyPatchInput,
    idempotencyKey: string,
  ): BackupPolicy {
    const user = this.requireOpsBackups(auth);
    const payload = JSON.stringify(input);
    return this.idempotent(
      idempotencyKey,
      payload,
      () => {
        // 恢复期间维护门禁：策略 PATCH 503 maintenance_mode（规格 §7 白名单）
        if (this.hasActiveRestore()) {
          throw new MockHttpError(503, 'maintenance_mode');
        }
        const policy = this.backupPolicy;
        if (policy.version !== input.expected_version) {
          throw new MockHttpError(409, 'version_conflict', { current_version: policy.version });
        }
        const next: StoredBackupPolicy = {
          enabled: input.enabled ?? policy.enabled,
          frequency: input.frequency ?? policy.frequency,
          localTime: input.local_time ?? policy.localTime,
          weekdays: input.weekdays !== undefined ? [...input.weekdays] : policy.weekdays,
          timezone: input.timezone ?? policy.timezone,
          keepLast: input.keep_last ?? policy.keepLast,
          retentionDays: input.retention_days ?? policy.retentionDays,
          version: policy.version + 1,
          lastScheduledFor: policy.lastScheduledFor,
          lastOutcome: policy.lastOutcome,
        };
        // 合并后整体验证：weekly 至少一天（规格 §4）
        if (next.frequency === 'weekly' && next.weekdays.length === 0) {
          throw new MockHttpError(422, 'validation_error', { field: 'weekdays' });
        }
        this.backupPolicy = next;
        return this.toBackupPolicy();
      },
      user.id,
      'backup-policy',
      'singleton',
    );
  }

  private toBackupItem(backup: StoredBackup): OpsBackupItem {
    return {
      backup_id: backup.backupId,
      status: backup.status,
      created_at: backup.createdAt,
      completed_at: backup.completedAt,
      restorable: backup.restorable,
    };
  }

  private toBackupDetail(backup: StoredBackup): OpsBackupDetail {
    return {
      ...this.toBackupItem(backup),
      components: backup.components.map((component) => ({
        kind: component.kind,
        status: component.status,
        reference: component.reference,
        failure_reason: component.failureReason,
      })),
    };
  }

  private toRestoreItem(restore: StoredRestore): OpsRestoreItem {
    return {
      restore_id: restore.restoreId,
      backup_id: restore.backupId,
      status: restore.status,
      created_at: restore.createdAt,
      completed_at: restore.completedAt,
    };
  }

  private toRestoreDetail(restore: StoredRestore): OpsRestoreDetail {
    return {
      ...this.toRestoreItem(restore),
      failure_reason: restore.failureReason,
      stages: restore.stages.map((stage) => ({ stage: stage.stage, status: stage.status })),
      repair_targets: restore.repairTargets.map((target) => ({
        target_id: target.targetId,
        stage: target.stage,
        resource_id: target.resourceId,
        status: target.status,
        failure_classification: target.failureClassification,
        attempts: target.attempts,
      })),
    };
  }

  private toBackupPolicy(): BackupPolicy {
    const policy = this.backupPolicy;
    return {
      enabled: policy.enabled,
      frequency: policy.frequency,
      local_time: policy.localTime,
      weekdays: [...policy.weekdays],
      timezone: policy.timezone,
      keep_last: policy.keepLast,
      retention_days: policy.retentionDays,
      version: policy.version,
      next_run_at: policy.enabled ? nextPolicyRunAt(policy.localTime) : null,
      last_scheduled_for: policy.lastScheduledFor,
      last_outcome: policy.lastOutcome,
    };
  }


  /* ---------- §11.1 排行榜 ---------- */

  getLeaderboard(auth: string | null): LeaderboardResponse {
    this.requireOpsOrAdmin(auth, 'evaluation_forbidden');
    return {
      entries: [
        {
          rank: 1,
          name: 'config-2026-07-b',
          score: 0.86,
          metrics: { faithfulness: 0.9, hit_at_k_final: 0.8 },
          eligible: true,
          is_active: true,
        },
        {
          rank: 2,
          name: 'config-2026-07-a',
          score: 0.83,
          metrics: { faithfulness: 0.85, hit_at_k_final: 0.78 },
          eligible: true,
          is_active: false,
        },
        {
          rank: 3,
          name: 'config-2026-06-c',
          score: 0.79,
          metrics: { faithfulness: 0.81, hit_at_k_final: 0.74 },
          eligible: false,
          is_active: false,
        },
      ],
      shadow_entries: [
        {
          rank: 1,
          name: 'shadow-cfg-3',
          score: 0.81,
          metrics: { answer_relevancy: 0.84 },
          eligible: true,
          is_active: false,
        },
        {
          rank: 2,
          name: 'shadow-cfg-2',
          score: 0.77,
          metrics: { answer_relevancy: 0.79 },
          eligible: false,
          is_active: false,
        },
      ],
      policy: { ...EVALUATION_POLICY },
    };
  }

  /* ---------- §11.2–11.3 校准窗口 ---------- */

  getCalibrationWindow(auth: string | null): CalibrationWindow {
    this.requireOpsOrAdmin(auth, 'calibration_forbidden');
    const window = this.calibrationWindow;
    if (window === null) {
      // 无 open/closing 窗口：合成 closed 读模型。
      return {
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
      };
    }
    return this.toCalibrationWindow(window);
  }

  postCalibrationWindow(
    auth: string | null,
    action: CalibrationWindowAction,
    windowKind: CalibrationWindowKind | null,
    idempotencyKey: string,
  ): CalibrationWindow {
    const user = this.auth(auth);
    if (user.role !== 'ops') {
      // 超管与其他角色调用 403（超管端不渲染开关控件，双重约束）。
      throw new MockHttpError(403, 'forbidden');
    }
    const payload = JSON.stringify({ action, windowKind });
    return this.idempotent(
      idempotencyKey,
      payload,
      () => {
        if (action === 'open') {
          if (windowKind === null) {
            throw new MockHttpError(422, 'validation_error', { field: 'window_kind' });
          }
          const current = this.calibrationWindow;
          if (current !== null && current.status === 'open') {
            throw new MockHttpError(409, 'calibration_window_already_open');
          }
          if (current !== null && current.status === 'closing') {
            throw new MockHttpError(409, 'calibration_window_closing');
          }
          if (windowKind !== 'manual' && !this.calibrationEligible) {
            throw new MockHttpError(409, 'calibration_window_not_eligible');
          }
          const created: StoredCalibrationWindow = {
            windowId: this.nextId('cw'),
            status: 'open',
            kind: windowKind,
            policyVersion: EVALUATION_POLICY.policy_version,
            sampleRate: this.sampleRateFor(windowKind),
            pairsCollected: 0,
            openedAt: new Date().toISOString(),
            closedAt: null,
            closeDeadlineAt: null,
            openedBy: user.id,
            closedBy: null,
          };
          this.calibrationWindow = created;
          return this.toCalibrationWindow(created);
        }
        const current = this.calibrationWindow;
        if (current === null || current.status !== 'open') {
          throw new MockHttpError(409, 'calibration_window_not_open');
        }
        current.status = 'closing';
        current.closeDeadlineAt = new Date(Date.now() + 24 * 3600 * 1000).toISOString();
        return this.toCalibrationWindow(current);
      },
      user.id,
      'calibration-window',
      'singleton',
    );
  }

  private sampleRateFor(kind: CalibrationWindowKind): number {
    switch (kind) {
      case 'cold_start':
        return EVALUATION_POLICY.cold_start_sample_rate;
      case 'sentinel':
        return EVALUATION_POLICY.sentinel_sample_rate;
      default:
        return 0.1;
    }
  }

  private toCalibrationWindow(window: StoredCalibrationWindow): CalibrationWindow {
    return {
      window_id: window.windowId,
      status: window.status,
      opened_at: window.openedAt,
      closed_at: window.closedAt,
      pairs_collected: window.pairsCollected,
      close_deadline_at: window.closeDeadlineAt,
      window_kind: window.kind,
      policy_version: window.policyVersion,
      sample_rate: window.sampleRate,
      opened_by: window.openedBy,
      closed_by: window.closedBy,
    };
  }

  /* ---------- §12.1–12.4 用户管理 ---------- */

  listUsers(
    auth: string | null,
    query: { q?: string; departmentId?: string; role?: Role; page?: number; pageSize?: number },
  ): { items: AdminUserItem[]; total: number; page: number; page_size: number } {
    const actor = this.auth(auth);
    if (actor.role !== 'admin' && actor.role !== 'ops') {
      throw new MockHttpError(403, 'admin_users_forbidden');
    }
    const page = query.page ?? 1;
    const pageSize = query.pageSize ?? 10;
    if (!Number.isInteger(page) || page < 1) {
      throw new MockHttpError(422, 'validation_error', { field: 'page' });
    }
    if (!Number.isInteger(pageSize) || pageSize < 1 || pageSize > 200) {
      throw new MockHttpError(422, 'validation_error', { field: 'page_size' });
    }
    const keyword = query.q?.trim().toLowerCase() ?? '';
    const all = [...this.adminUsers.values()]
      // 默认列表含 active 与 pending_delete（只读），不含 deleted 墓碑。
      .filter((record) => record.lifecycle !== 'deleted')
      .filter((record) => query.departmentId === undefined || record.department?.id === query.departmentId)
      .filter((record) => query.role === undefined || record.role === query.role)
      .filter((record) => keyword === '' || this.userMatchesKeyword(record, keyword));
    const start = (page - 1) * pageSize;
    return {
      items: all.slice(start, start + pageSize).map((record) => this.toAdminUserItem(record)),
      total: all.length,
      page,
      page_size: pageSize,
    };
  }

  /** 聚合查找：单输入匹配姓名 / 显示名 / 用户名 / 部门名 / 角色名（机读与中文标签）。 */
  private userMatchesKeyword(record: StoredAdminUser, keyword: string): boolean {
    const haystacks = [
      record.realName,
      record.displayName,
      record.username,
      record.department?.name ?? '',
      record.role,
      ROLE_LABELS[record.role],
    ];
    return haystacks.some((value) => value.toLowerCase().includes(keyword));
  }

  createUser(
    auth: string | null,
    input: {
      username: string;
      real_name: string;
      display_name?: string;
      department_id: string | null;
      role: Role;
      initial_password: string;
    },
  ): AdminUserItem {
    const actor = this.auth(auth);
    if (actor.role !== 'admin' && actor.role !== 'ops') {
      throw new MockHttpError(403, 'forbidden_target');
    }
    // 用户名永久占用：deleted 墓碑保留用户名，创建同名账号继续 409。
    const conflict = [...this.adminUsers.values()].some((record) => record.username === input.username);
    if (conflict) {
      throw new MockHttpError(409, 'username_exists');
    }
    // 角色按操作者权限受限；admin 角色仅经部署清单，运行时一律拒绝。
    if (input.role === 'admin' || !this.creatableRoles(actor).includes(input.role)) {
      throw new MockHttpError(403, 'forbidden_target');
    }
    const department = this.resolveTargetDepartment(input.department_id);
    if (input.role === 'minister' && department === null) {
      throw new MockHttpError(422, 'minister_department_required');
    }
    const now = new Date().toISOString();
    const record: StoredAdminUser = {
      id: this.nextId('u'),
      username: input.username,
      realName: input.real_name,
      displayName: input.display_name ?? input.real_name,
      department,
      role: input.role,
      lastActiveAt: null,
      documentCount: 0,
      version: 1,
      lifecycle: 'active',
      deletionRequestedAt: null,
      purgeAfterAt: null,
    };
    record.lastActiveAt = now;
    this.adminUsers.set(record.id, record);
    return this.toAdminUserItem(record);
  }

  patchUser(
    auth: string | null,
    userId: string,
    input: { expected_version: number; role?: Role; department_id?: string | null },
  ): AdminUserItem {
    const actor = this.auth(auth);
    if (actor.role !== 'admin' && actor.role !== 'ops') {
      throw new MockHttpError(403, 'forbidden_target');
    }
    const target = this.requireManageableTarget(actor, userId);
    if (target.version !== input.expected_version) {
      throw new MockHttpError(409, 'version_conflict');
    }
    const nextRole = input.role ?? target.role;
    // 角色切换范围：admin 操作 user/minister/ops；ops 操作 user/minister。
    if (!this.creatableRoles(actor).includes(nextRole)) {
      throw new MockHttpError(403, 'forbidden_target');
    }
    // 未提交 department_id 保持原值；显式 null 解除部门归属。
    const nextDepartment = Object.hasOwn(input, 'department_id')
      ? this.resolveTargetDepartment(input.department_id ?? null)
      : target.department;
    // 原子校验最终 (role, department)：minister 必须绑定 active 部门。
    if (nextRole === 'minister' && nextDepartment === null) {
      throw new MockHttpError(422, 'minister_department_required');
    }
    target.role = nextRole;
    target.department = nextDepartment;
    target.version += 1;
    // TODO：角色或部门变更成功后目标用户全部设备会话应由服务端撤销（契约 §12.3）。
    // 当前 MockAuthController 仅支持操作者本人 revokeAllSessions，无管理侧按 userId
    // 撤销能力；后端负责该事务副作用，mock 暂不落会话联动。
    return this.toAdminUserItem(target);
  }

  deleteUser(auth: string | null, userId: string, expectedVersion: number): {
    id: string;
    version: number;
    lifecycle_status: 'pending_delete';
    deletion_requested_at: string;
    purge_after_at: string;
  } {
    const actor = this.auth(auth);
    if (actor.role !== 'admin' && actor.role !== 'ops') {
      throw new MockHttpError(403, 'forbidden_target');
    }
    const target = this.requireManageableTarget(actor, userId);
    if (target.version !== expectedVersion) {
      throw new MockHttpError(409, 'version_conflict');
    }
    const now = Date.now();
    target.lifecycle = 'pending_delete';
    target.deletionRequestedAt = new Date(now).toISOString();
    target.purgeAfterAt = new Date(now + PURGE_RETENTION_MS).toISOString();
    target.version += 1;
    // TODO：删除事务内撤销目标全部认证会话由后端负责（契约 §12.4）；mock 会话联动同上。
    return {
      id: target.id,
      version: target.version,
      lifecycle_status: 'pending_delete',
      deletion_requested_at: target.deletionRequestedAt,
      purge_after_at: target.purgeAfterAt,
    };
  }

  /** 管理目标守卫：admin 目标 / 自身 / 运维越权目标 / 冻结目标逐一拦截。 */
  private requireManageableTarget(actor: User, userId: string): StoredAdminUser {
    const target = this.adminUsers.get(userId);
    if (target === undefined) {
      throw new MockHttpError(404, 'user_not_found');
    }
    // admin 账号的管理侧修改仅经部署清单，运行时接口对任何 admin 目标 403。
    if (target.role === 'admin') {
      throw new MockHttpError(403, 'forbidden_target');
    }
    if (target.id === actor.id) {
      throw new MockHttpError(403, 'cannot_modify_self');
    }
    // 运维仅可管理普通用户与部长（目标视角）。
    if (actor.role === 'ops' && !this.creatableRoles(actor).includes(target.role)) {
      throw new MockHttpError(403, 'forbidden_target');
    }
    if (target.lifecycle === 'pending_delete') {
      throw new MockHttpError(409, 'user_pending_delete');
    }
    return target;
  }

  /** 操作者可创建 / 可切换的角色集（admin: user/minister/ops；ops: user/minister）。 */
  private creatableRoles(actor: User): Role[] {
    return actor.role === 'admin' ? ['user', 'minister', 'ops'] : ['user', 'minister'];
  }

  /** 部门归属校验：null → 无部门；不存在 404；inactive 409。 */
  private resolveTargetDepartment(departmentId: string | null): { id: string; name: string } | null {
    if (departmentId === null) {
      return null;
    }
    const department = this.departments.get(departmentId);
    if (department === undefined) {
      throw new MockHttpError(404, 'department_not_found');
    }
    if (department.status !== 'active') {
      throw new MockHttpError(409, 'department_inactive');
    }
    return { id: department.id, name: department.name };
  }

  /* ---------- §12.5 部门目录与部门管理 ---------- */

  listDepartments(auth: string | null, status: DepartmentStatusFilter = 'active'): { items: AdminDepartmentItem[] } {
    const actor = this.auth(auth);
    if (actor.role !== 'admin' && actor.role !== 'ops') {
      throw new MockHttpError(403, 'department_action_forbidden');
    }
    if (status !== 'active' && status !== 'inactive' && status !== 'all') {
      throw new MockHttpError(422, 'validation_error', { field: 'status' });
    }
    const items = [...this.departments.values()]
      .filter((department) => status === 'all' || department.status === status)
      .map((department) => this.toDepartmentItem(actor, department));
    return { items };
  }

  createDepartment(auth: string | null, name: string, idempotencyKey: string): AdminDepartmentItem {
    const actor = this.requireDepartmentWriter(auth);
    const payload = JSON.stringify({ name });
    return this.idempotent(
      idempotencyKey,
      payload,
      () => {
        this.requireUniqueDepartmentName(name);
        const department: StoredDepartment = {
          id: this.nextId('d'),
          name,
          status: 'active',
          version: 1,
          documentCount: 0,
          memberCount: 0,
          nonterminalJobCount: 0,
          pendingSubmissionCount: 0,
          deactivatedAt: null,
        };
        this.departments.set(department.id, department);
        return this.toDepartmentItem(actor, department);
      },
      actor.id,
      'department-create',
      'directory',
    );
  }

  renameDepartment(
    auth: string | null,
    departmentId: string,
    expectedVersion: number,
    name: string,
    idempotencyKey: string,
  ): AdminDepartmentItem {
    const actor = this.requireDepartmentWriter(auth);
    const payload = JSON.stringify({ departmentId, expectedVersion, name });
    return this.idempotent(
      idempotencyKey,
      payload,
      () => {
        const department = this.requireActiveDepartment(departmentId);
        if (department.version !== expectedVersion) {
          throw new MockHttpError(409, 'version_conflict', { current_version: department.version });
        }
        this.requireUniqueDepartmentName(name);
        department.name = name;
        department.version += 1;
        return this.toDepartmentItem(actor, department);
      },
      actor.id,
      'department-rename',
      departmentId,
    );
  }

  deactivateDepartment(
    auth: string | null,
    departmentId: string,
    expectedVersion: number,
    idempotencyKey: string,
  ): AdminDepartmentItem {
    const actor = this.requireDepartmentWriter(auth);
    const payload = JSON.stringify({ departmentId, expectedVersion });
    return this.idempotent(
      idempotencyKey,
      payload,
      () => {
        const department = this.requireActiveDepartment(departmentId);
        if (department.version !== expectedVersion) {
          throw new MockHttpError(409, 'version_conflict', { current_version: department.version });
        }
        // 停用前置校验全在服务端；被阻断后不出现「强制停用」替代入口。
        if (this.deactivationUnverified.has(departmentId)) {
          throw new MockHttpError(503, 'department_deactivation_unverified', { retryable: true });
        }
        if (department.memberCount > 0) {
          throw new MockHttpError(409, 'department_has_members');
        }
        if (department.nonterminalJobCount > 0 || department.pendingSubmissionCount > 0) {
          throw new MockHttpError(409, 'department_has_active_work');
        }
        department.status = 'inactive';
        department.deactivatedAt = new Date().toISOString();
        department.version += 1;
        return this.toDepartmentItem(actor, department);
      },
      actor.id,
      'department-deactivate',
      departmentId,
    );
  }

  private requireDepartmentWriter(auth: string | null): User {
    const actor = this.auth(auth);
    if (actor.role !== 'admin') {
      throw new MockHttpError(403, 'department_action_forbidden');
    }
    return actor;
  }

  private requireActiveDepartment(departmentId: string): StoredDepartment {
    const department = this.departments.get(departmentId);
    if (department === undefined) {
      throw new MockHttpError(404, 'department_not_found');
    }
    if (department.status !== 'active') {
      throw new MockHttpError(409, 'department_inactive');
    }
    return department;
  }

  /** active/inactive 全生命周期名称唯一（服务端规范化后检查）。 */
  private requireUniqueDepartmentName(name: string): void {
    const normalized = name.trim().toLowerCase();
    for (const department of this.departments.values()) {
      if (department.name.trim().toLowerCase() === normalized) {
        throw new MockHttpError(409, 'department_name_exists');
      }
    }
  }

  /** allowed_actions 唯一依据：admin 的 active 部门可 rename/deactivate，其余一律 []。 */
  private toDepartmentItem(actor: User, department: StoredDepartment): AdminDepartmentItem {
    const manageable = actor.role === 'admin' && department.status === 'active';
    return {
      id: department.id,
      name: department.name,
      status: department.status,
      version: department.version,
      document_count: department.documentCount,
      member_count: department.memberCount,
      nonterminal_job_count: department.nonterminalJobCount,
      pending_submission_count: department.pendingSubmissionCount,
      deactivated_at: department.deactivatedAt,
      allowed_actions: manageable ? ['rename', 'deactivate'] : [],
    };
  }

  /* ---------- §12.7 权限矩阵（超管只读；内容即后端设计 §9.2 矩阵） ---------- */

  getPermissionMatrix(auth: string | null): PermissionMatrixResponse {
    const actor = this.auth(auth);
    if (actor.role !== 'admin') {
      throw new MockHttpError(403, 'permission_matrix_forbidden');
    }
    const row = (
      key: string,
      label: string,
      user: boolean,
      minister: boolean,
      ops: boolean,
      admin: boolean,
    ) => ({ key, label, roles: { user, minister, ops, admin } });
    return {
      capabilities: [
        row('query_personal_space', '查询自己的个人库', true, true, true, true),
        row('query_public_space', '查询公共库', true, true, true, true),
        row('query_own_department_space', '查询本部门的部门库', true, true, true, true),
        row('query_other_department_space', '查询其他部门的部门库', false, false, true, true),
        row('upload_personal_space', '上传文档到自己的个人库', true, true, true, true),
        row('contribute_own_department', '向本部门部门库添加内容', true, true, true, true),
        row('contribute_other_department', '向其他部门部门库添加内容', false, false, true, true),
        row('manage_own_department_space', '管理本部门的部门库', false, true, true, true),
        row('manage_other_department_space', '管理其他部门的部门库', false, false, true, true),
        row('view_others_personal_space', '查看他人个人库', false, false, true, true),
        row('contribute_public_space', '向公共库添加内容', true, true, true, true),
        row('review_department_submissions', '审核部门库投稿', false, true, false, true),
        row('review_public_submissions', '审核公共库投稿', false, false, true, true),
        row('chat_qa', '使用聊天问答', true, true, true, true),
        row('manage_user_accounts', '用户账号管理', false, false, true, true),
        row('read_department_directory', '读取部门目录', false, false, true, true),
        row('manage_department_directory', '管理部门目录', false, false, false, true),
        row('build_public_graph', '构建或重建公共库图谱', false, false, true, false),
        row('instance_operations', '实例运维操作', false, false, true, true),
        row('read_permission_matrix', '读取角色与能力矩阵', false, false, false, true),
      ],
    };
  }

  /* ---------- 内部：dashboard 组包 ---------- */

  private buildOpsDashboard(window: MetricsWindow): DashboardResponse {
    const scale = windowScale(window);
    const quotaPending = [...this.quotaApprovals.values()].filter(
      (request) => request.status === 'pending',
    ).length;
    const pendingPublicSubmissions = this.pendingPublicSubmissionCount();
    return {
      window,
      packs: [
        {
          key: 'tasks_health',
          title: '任务与健康',
          cards: [
            {
              key: 'ingestion_backlog',
              title: '入库队列积压',
              kind: 'stat',
              value: 26,
              delta: { direction: 'up', text_hint: '+3' },
              sparkline: [4, 6, 5, 9, 12],
              threshold: { value: 20, direction: 'above' },
              link: 'ops.jobs',
            },
            {
              key: 'failure_rate',
              title: '失败率',
              kind: 'stat',
              value: round2(4 * scale),
              delta: { direction: 'down', text_hint: '-1.2' },
              sparkline: scaledSeries([7, 6, 5, 5, 4], scale),
              threshold: { value: 10, direction: 'above' },
              link: 'ops.jobs',
            },
            {
              key: 'stale_recoveries',
              title: '超时回收次数',
              kind: 'stat',
              value: Math.round(1 * scale),
              delta: { direction: 'flat', text_hint: '0' },
              sparkline: scaledSeries([2, 1, 1, 0, 1], scale),
              threshold: { value: 5, direction: 'above' },
              link: 'ops.jobs',
            },
            {
              key: 'api_error_rate',
              title: 'API 错误率',
              kind: 'stat',
              value: round2(0.4 * scale),
              delta: { direction: 'flat', text_hint: '0' },
              sparkline: scaledSeries([0.3, 0.5, 0.4, 0.4, 0.4], 1),
              threshold: { value: 2, direction: 'above' },
              link: 'ops.metrics',
            },
            {
              key: 'api_latency',
              title: 'API 延迟',
              kind: 'stat',
              value: null,
              delta: null,
              sparkline: [],
              threshold: { value: 800, direction: 'above' },
              link: 'ops.metrics',
            },
          ],
        },
        {
          key: 'cost_sentinel',
          title: '成本哨兵',
          cards: [
            {
              key: 'cache_hit_rate',
              title: '缓存命中率',
              kind: 'stat',
              value: round2(0.72 - (scale - 1) * 0.05),
              delta: { direction: 'up', text_hint: '+3.1%' },
              sparkline: scaledSeries([0.6, 0.64, 0.66, 0.7, 0.72], 1),
              threshold: { value: 0.6, direction: 'below' },
              link: 'ops.metrics',
            },
            {
              key: 'llm_cost_trend',
              title: 'LLM 调用量与成本趋势',
              kind: 'stat',
              value: Math.round(1280 * scale),
              delta: { direction: 'up', text_hint: '+6.2%' },
              sparkline: scaledSeries([900, 980, 1050, 1180, 1280], scale),
              threshold: null,
              link: 'ops.metrics',
            },
          ],
        },
        {
          key: 'ingestion_quality',
          title: '入库质量',
          cards: [
            {
              key: 'ocr_confidence_dist',
              title: 'OCR 置信度分布',
              kind: 'distribution',
              rows: [
                { label: '90–100%', value: Math.round(82 * scale), ratio: 0.82, tone: 'normal' },
                { label: '<90%', value: Math.round(18 * scale), ratio: 0.18, tone: 'warning' },
              ],
              threshold: null,
              link: 'ops.metrics',
            },
            {
              key: 'low_confidence_doc_ratio',
              title: '低置信文档占比',
              kind: 'distribution',
              rows: [
                { label: '低置信', value: Math.round(12 * scale), ratio: 0.12, tone: 'warning' },
                { label: '正常', value: Math.round(88 * scale), ratio: 0.88, tone: 'normal' },
              ],
              threshold: null,
              link: 'ops.metrics',
            },
            {
              key: 'graph_basic_split',
              title: '建树 / basic 分流比例',
              kind: 'distribution',
              rows: [
                { label: '建树', value: Math.round(35 * scale), ratio: 0.35, tone: 'normal' },
                { label: 'basic', value: Math.round(65 * scale), ratio: 0.65, tone: 'normal' },
              ],
              threshold: null,
              link: 'ops.metrics',
            },
          ],
        },
        {
          key: 'todo',
          title: '待办',
          cards: [
            {
              key: 'quota_pending',
              title: '配额申请待处理数',
              kind: 'count',
              value: quotaPending,
              delta: null,
              threshold: null,
              link: 'ops.approvals.quota',
            },
            {
              key: 'submission_pending',
              title: '投稿审核待处理数',
              kind: 'count',
              value: pendingPublicSubmissions,
              delta: null,
              threshold: null,
              link: 'ops.approvals.submissions',
            },
          ],
        },
      ],
    };
  }

  private buildAdminDashboard(window: MetricsWindow, expandUserRank = false): DashboardResponse {
    const scale = windowScale(window);
    const rankRows = Array.from({ length: 15 }, (_unused, index) => {
      const value = Math.round((900 - index * 52) * scale);
      return { label: `user_${(index + 1).toString().padStart(2, '0')}`, value, ratio: round2(value / (900 * scale)) };
    });
    // pack.description 是契约外的展示便利字段（超管四包各一条 15px slate 说明行，
    // 文案取 copy.admin.dashboard.packs；运维包不带）。后端按角色组包时同义下发，
    // 前端有则渲染、不做 pack.key 角色判定。
    const packDescriptions = copy.admin.dashboard.packs;
    return {
      window,
      packs: [
        {
          key: 'usage_overview',
          title: '使用概览',
          description: packDescriptions.usageOverview,
          cards: [
            {
              key: 'active_users',
              title: '活跃用户数',
              kind: 'stat',
              value: Math.round(86 * scale),
              delta: { direction: 'up', text_hint: '+12' },
              sparkline: scaledSeries([60, 66, 72, 80, 86], scale),
              threshold: null,
              link: null,
            },
            {
              key: 'question_trend',
              title: '提问量趋势',
              kind: 'stat',
              value: Math.round(342 * scale),
              delta: { direction: 'down', text_hint: '-18' },
              sparkline: scaledSeries([300, 320, 310, 330, 342], scale),
              threshold: null,
              link: null,
            },
            {
              key: 'department_usage',
              title: '按部门使用分布',
              kind: 'distribution',
              rows: [
                { label: '财务部', value: Math.round(120 * scale), ratio: 0.4, tone: 'normal' },
                { label: '人事部', value: Math.round(90 * scale), ratio: 0.3, tone: 'normal' },
                { label: '未分配', value: Math.round(60 * scale), ratio: 0.2, tone: 'normal' },
                { label: '其他', value: Math.round(30 * scale), ratio: 0.1, tone: 'normal' },
              ],
              threshold: null,
              link: null,
            },
          ],
        },
        {
          key: 'asset_usage',
          title: '知识资产使用率',
          description: packDescriptions.assetUsage,
          cards: [
            {
              key: 'retrieval_freq',
              title: '各空间被检索频次分布',
              kind: 'distribution',
              rows: [
                { label: '公共库', value: Math.round(210 * scale), ratio: 0.55, tone: 'normal' },
                { label: '财务部', value: Math.round(90 * scale), ratio: 0.24, tone: 'normal' },
                { label: '人事部', value: Math.round(50 * scale), ratio: 0.13, tone: 'normal' },
                { label: '个人库', value: Math.round(30 * scale), ratio: 0.08, tone: 'normal' },
              ],
              threshold: null,
              link: null,
            },
            {
              key: 'citation_freq',
              title: '各空间被引用频次分布',
              kind: 'distribution',
              rows: [
                { label: '公共库', value: Math.round(150 * scale), ratio: 0.6, tone: 'normal' },
                { label: '财务部', value: Math.round(60 * scale), ratio: 0.24, tone: 'normal' },
                { label: '人事部', value: Math.round(25 * scale), ratio: 0.1, tone: 'normal' },
                { label: '个人库', value: Math.round(15 * scale), ratio: 0.06, tone: 'normal' },
              ],
              threshold: null,
              link: null,
            },
          ],
        },
        {
          key: 'cost_share',
          title: '成本分摊',
          description: packDescriptions.costShare,
          cards: [
            {
              key: 'monthly_llm_cost',
              title: '月度 LLM 总成本',
              kind: 'stat',
              value: Math.round(4520 * scale),
              delta: { direction: 'up', text_hint: '+8.4%' },
              sparkline: scaledSeries([3200, 3600, 3900, 4200, 4520], scale),
              threshold: null,
              link: null,
            },
            {
              key: 'department_cost',
              title: '按部门分摊',
              kind: 'distribution',
              rows: [
                { label: '财务部', value: Math.round(1800 * scale), ratio: 0.4, tone: 'normal' },
                { label: '人事部', value: Math.round(1350 * scale), ratio: 0.3, tone: 'normal' },
                { label: '无部门', value: Math.round(900 * scale), ratio: 0.2, tone: 'normal' },
                { label: '运维', value: Math.round(450 * scale), ratio: 0.1, tone: 'normal' },
              ],
              threshold: null,
              link: null,
            },
            {
              key: 'user_cost_rank',
              title: '按用户分摊',
              kind: 'user_rank',
              rows: expandUserRank ? rankRows : rankRows.slice(0, 10),
              total_count: rankRows.length,
              threshold: null,
              link: null,
            },
          ],
        },
        {
          key: 'quality_quota',
          title: '质量与配额',
          description: packDescriptions.qualityQuota,
          cards: [
            {
              key: 'thumbs_up_ratio',
              title: '点赞比例趋势',
              kind: 'stat',
              value: 0.86,
              delta: { direction: 'flat', text_hint: '0' },
              sparkline: [0.82, 0.84, 0.85, 0.86, 0.86],
              threshold: null,
              link: null,
            },
            {
              key: 'quota_consumption',
              title: '配额消耗分布',
              kind: 'distribution',
              rows: [
                { label: '财务部', value: Math.round(320 * scale), ratio: 0.45, tone: 'normal' },
                { label: '人事部', value: Math.round(210 * scale), ratio: 0.3, tone: 'normal' },
                { label: '其他', value: Math.round(180 * scale), ratio: 0.25, tone: 'normal' },
              ],
              threshold: null,
              link: null,
            },
            {
              key: 'quota_grants',
              title: '追加额度发放',
              kind: 'distribution',
              rows: [
                { label: '追加次数', value: Math.round(6 * scale), ratio: 0.3, tone: 'normal' },
                { label: '追加页数', value: Math.round(1400 * scale), ratio: 1, tone: 'normal' },
              ],
              threshold: null,
              link: null,
            },
          ],
        },
      ],
    };
  }

  /** dashboard 待办卡联动：公共库待审投稿数（ops 审核范围计数，与 §8.1 同口径）。 */
  private pendingPublicSubmissionCount(): number {
    return this.knowledge.countPendingSubmissions(['public']);
  }

  /* ---------- 内部 ---------- */

  private requireOpsOrAdmin(auth: string | null, code: string): User {
    const user = this.auth(auth);
    if (user.role !== 'ops' && user.role !== 'admin') {
      throw new MockHttpError(403, code);
    }
    return user;
  }

  private requireWindow(window: MetricsWindow): void {
    if (window !== 'today' && window !== '7d' && window !== '30d') {
      throw new MockHttpError(422, 'validation_error', { field: 'window' });
    }
  }

  private toQuotaRequestItem(request: StoredQuotaApproval): QuotaRequestItem {
    return {
      id: request.id,
      version: request.version,
      status: request.status,
      applicant: { id: request.applicantId, display_name: request.applicantName },
      current_usage: { used: request.used, effective_limit: request.effectiveLimit },
      requested_pages: request.requestedPages,
      approved_pages: request.approvedPages,
      quota_period: request.quotaPeriod,
      created_at: request.createdAt,
      reviewed_at: request.reviewedAt,
    };
  }

  private toAdminUserItem(record: StoredAdminUser): AdminUserItem {
    return {
      id: record.id,
      username: record.username,
      real_name: record.realName,
      display_name: record.displayName,
      department: record.department,
      role: record.role,
      last_active_at: record.lastActiveAt,
      document_count: record.documentCount,
      version: record.version,
      lifecycle_status: record.lifecycle,
      deletion_requested_at: record.deletionRequestedAt,
      purge_after_at: record.purgeAfterAt,
    };
  }

  private seedFixtures(): void {
    // 配额申请：zhangsan / minister-li 各一条 pending；ghost（冻结账号）一条 pending
    // （quota_request_not_approvable 路径）；zhangsan 一条已批准历史（status 过滤演示）。
    this.seedQuotaApproval('u_user', 'zhangsan', 120, 500, 100, 'pending', '2026-07-28T01:00:00Z');
    this.seedQuotaApproval('u_minister', 'minister-li', 120, 500, 200, 'pending', '2026-07-28T02:00:00Z');
    this.seedQuotaApproval('u_ghost', 'ghost', 0, 500, 50, 'pending', '2026-07-28T03:00:00Z');
    const history = this.seedQuotaApproval('u_user', 'zhangsan', 120, 500, 80, 'approved', '2026-07-01T01:00:00Z');
    history.approvedPages = 80;
    history.reviewedAt = '2026-07-01T02:00:00Z';

    // 用户目录：四角色 + 无部门用户 + 冻结行（含 purge_after_at）+
    // 被部署清单移除的 pending_delete admin 行；默认列表不含 deleted 墓碑。
    this.seedAdminUser('u_user', 'zhangsan', '张三', 'user', { id: 'd_finance', name: '财务部' }, 3);
    this.seedAdminUser('u_minister', 'minister-li', '李部长', 'minister', { id: 'd_finance', name: '财务部' }, 1);
    this.seedAdminUser('u_ops', 'ops-wang', '王运维', 'ops', null, 0);
    this.seedAdminUser('u_admin', 'admin', '系统管理员', 'admin', null, 0);
    this.seedAdminUser('u_chen', 'chenchen', '陈晨', 'user', { id: 'd_hr', name: '人事部' }, 2);
    this.seedAdminUser('u_zhao', 'zhaoliu', '赵六', 'user', null, 0);
    this.seedAdminUser('u_sun', 'sunqi', '孙琪', 'minister', { id: 'd_hr', name: '人事部' }, 0);
    const ghost = this.seedAdminUser('u_ghost', 'ghost', '鬼影', 'user', null, 0);
    ghost.lifecycle = 'pending_delete';
    ghost.deletionRequestedAt = '2026-07-20T08:00:00Z';
    ghost.purgeAfterAt = '2026-08-19T08:00:00Z';
    const formerAdmin = this.seedAdminUser('u_admin_former', 'former-admin', '前管理员', 'admin', null, 0);
    formerAdmin.lifecycle = 'pending_delete';
    formerAdmin.deletionRequestedAt = '2026-07-22T08:00:00Z';
    formerAdmin.purgeAfterAt = '2026-08-21T08:00:00Z';

    // 部门目录：d_finance 有成员（has_members 阻断）、d_hr 有非终态任务（has_active_work 阻断）、
    // d_empty 可正常停用、d_legacy 已停用（inactive 行）。
    this.seedDepartment('d_finance', '财务部', 'active', { documentCount: 2, memberCount: 3, nonterminalJobCount: 0, pendingSubmissionCount: 2 });
    this.seedDepartment('d_hr', '人事部', 'active', { documentCount: 1, memberCount: 0, nonterminalJobCount: 1, pendingSubmissionCount: 1 });
    this.seedDepartment('d_empty', '空壳部', 'active', { documentCount: 0, memberCount: 0, nonterminalJobCount: 0, pendingSubmissionCount: 0 });
    this.seedDepartment('d_legacy', '档案部', 'inactive', { documentCount: 4, memberCount: 0, nonterminalJobCount: 0, pendingSubmissionCount: 0 });

    // 图谱投影默认种子：stale（公共库已变更需重建）+ 上一可用 generation，latest_run 空。
    this.graph = {
      sourceRevision: 12,
      availability: 'stale',
      activeGeneration: {
        graph_generation_id: 'gg_11',
        source_revision: 11,
        built_at: '2026-07-30T08:00:00Z',
      },
      latestRun: null,
    };

    // 备份与恢复种子：一份 complete 可恢复备份（恢复来源可选）、一份 creating 备份
    // （首轮读仍 creating，下一次读推进 complete，供轮询收敛演示）、一条 blocked 恢复
    // （含 open 修复目标，retry 受理后下一轮读转 succeeded）、一份默认策略（daily 02:30）。
    this.seedBackup({
      backupId: OPS_BACKUP_SEED_IDS.completeBackup,
      status: 'complete',
      createdAt: '2026-08-17T18:30:00Z',
      completedAt: '2026-08-17T18:32:00Z',
      restorable: true,
      components: [
        { kind: 'postgres_snapshot', status: 'complete', reference: 'pg:bk_seed_complete', failureReason: null },
        { kind: 'object_store_snapshot', status: 'complete', reference: 'obj:bk_seed_complete', failureReason: null },
        { kind: 'object_manifest', status: 'complete', reference: 'manifest:128', failureReason: null },
      ],
    });
    this.seedBackup({
      backupId: OPS_BACKUP_SEED_IDS.creatingBackup,
      status: 'creating',
      createdAt: '2026-08-19T02:00:00Z',
      completedAt: null,
      restorable: false,
      components: OPS_BACKUP_COMPONENT_KINDS.map((kind) => ({
        kind,
        status: 'creating' as const,
        reference: null,
        failureReason: null,
      })),
    });
    this.restores.set(OPS_BACKUP_SEED_IDS.repairRestore, {
      restoreId: OPS_BACKUP_SEED_IDS.repairRestore,
      backupId: OPS_BACKUP_SEED_IDS.completeBackup,
      status: 'blocked',
      createdAt: '2026-08-18T03:00:00Z',
      completedAt: null,
      failureReason: null,
      stages: [
        { stage: 'postgres', status: 'succeeded' },
        { stage: 'object_store', status: 'succeeded' },
        { stage: 'milvus', status: 'failed' },
        { stage: 'sparse', status: 'pending' },
        { stage: 'summary', status: 'pending' },
        { stage: 'graph', status: 'pending' },
        { stage: 'cache', status: 'pending' },
      ],
      repairTargets: [
        {
          targetId: OPS_BACKUP_SEED_IDS.repairTarget,
          stage: 'milvus',
          resourceId: 'doc_seed_1',
          status: 'open',
          failureClassification: 'checksum_mismatch',
          attempts: 1,
        },
      ],
    });
    this.backupPolicy = {
      enabled: true,
      frequency: 'daily',
      localTime: '02:30',
      weekdays: [0, 1, 2, 3, 4],
      timezone: 'Asia/Shanghai',
      keepLast: 7,
      retentionDays: 30,
      version: 1,
      lastScheduledFor: '2026-08-18T18:30:00Z',
      lastOutcome: 'succeeded',
    };
  }

  private seedBackup(input: {
    readonly backupId: string;
    readonly status: 'creating' | 'complete' | 'failed';
    readonly createdAt: string;
    readonly completedAt: string | null;
    readonly restorable: boolean;
    readonly components: readonly StoredBackupComponent[];
  }): void {
    this.backups.set(input.backupId, {
      backupId: input.backupId,
      status: input.status,
      createdAt: input.createdAt,
      completedAt: input.completedAt,
      restorable: input.restorable,
      components: input.components.map((component) => ({ ...component })),
      initiatorId: 'u_ops',
    });
  }

  private seedQuotaApproval(
    applicantId: string,
    applicantName: string,
    used: number,
    effectiveLimit: number,
    requestedPages: number,
    status: QuotaRequestStatus,
    createdAt: string,
  ): StoredQuotaApproval {
    const request: StoredQuotaApproval = {
      id: this.nextId('qr'),
      version: 1,
      status,
      applicantId,
      applicantName,
      used,
      effectiveLimit,
      requestedPages,
      approvedPages: null,
      quotaPeriod: '2026-08',
      createdAt,
      reviewedAt: null,
    };
    this.quotaApprovals.set(request.id, request);
    return request;
  }

  private seedAdminUser(
    id: string,
    username: string,
    realName: string,
    role: Role,
    department: { id: string; name: string } | null,
    documentCount: number,
  ): StoredAdminUser {
    const record: StoredAdminUser = {
      id,
      username,
      realName,
      displayName: realName,
      department,
      role,
      lastActiveAt: '2026-08-04T09:00:00Z',
      documentCount,
      version: 1,
      lifecycle: 'active',
      deletionRequestedAt: null,
      purgeAfterAt: null,
    };
    this.adminUsers.set(id, record);
    return record;
  }

  private seedDepartment(
    id: string,
    name: string,
    status: 'active' | 'inactive',
    counts: {
      readonly documentCount: number;
      readonly memberCount: number;
      readonly nonterminalJobCount: number;
      readonly pendingSubmissionCount: number;
    },
  ): StoredDepartment {
    const department: StoredDepartment = {
      id,
      name,
      status,
      version: 1,
      documentCount: counts.documentCount,
      memberCount: counts.memberCount,
      nonterminalJobCount: counts.nonterminalJobCount,
      pendingSubmissionCount: counts.pendingSubmissionCount,
      deactivatedAt: status === 'inactive' ? '2026-06-15T00:00:00Z' : null,
    };
    this.departments.set(id, department);
    return department;
  }

  private nextId(prefix: string): string {
    this.seq += 1;
    return `${prefix}_${this.seq.toString(36)}`;
  }
}

/* ---------- dashboard 数据缩放（window 三档返回不同数据，供切换动效验证） ---------- */

function windowScale(window: MetricsWindow): number {
  switch (window) {
    case 'today':
      return 0.4;
    case '30d':
      return 2.6;
    default:
      return 1;
  }
}

function scaledSeries(base: readonly number[], scale: number): number[] {
  return base.map((value) => round2(value * scale));
}

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}

/*
 * e2e 断言用账号 / 部门种子名称（copy-discipline：e2e 受 CJK 扫描，种子名称从 mock
 * 常量引用，与 NOTIFICATION_SEED_TITLES 同一约定）。值必须与 seedFixtures 一致。
 */
export const ADMIN_SEED_NAMES = {
  zhangsan: '张三',
  systemAdmin: '系统管理员',
  ministerLi: '李部长',
  chenchen: '陈晨',
  ghost: '鬼影',
  finance: '财务部',
  hr: '人事部',
  emptyDept: '空壳部',
} as const;

/*
 * e2e 断言用备份 / 恢复种子 ID（copy-discipline：与 ADMIN_SEED_NAMES 同一约定，
 * 值必须与 seedFixtures 一致）。
 */
export const OPS_BACKUP_SEED_IDS = {
  completeBackup: 'bk_seed_complete',
  creatingBackup: 'bk_seed_creating',
  repairRestore: 'rs_seed_repair',
  repairTarget: 'rt_seed_1',
} as const;
