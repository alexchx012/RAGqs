/*
 * 管理面板域 API 封装（契约 §6.12、§8.1–8.5、§9.1–9.2、§10.1–10.2、§11.1–11.3、§12.1–12.7）。
 * 与 settings/api.ts 同一约定：经 ApiClient 携带 /v1 前缀与 Bearer；写操作的
 * Idempotency-Key 由调用方生成并传入，本层原样透传；全部请求（含读）在发起时
 * capture 当前逻辑会话 authSessionGuard，响应前后由 client fail-closed 校验。
 * 复用边界（不重复定义）：
 * - 投稿内容受控查看 GET /submissions/{id}/content 复用 settings api.getSubmissionContent；
 * - 任务行操作 POST /ingestion-jobs/{id}/cancel|replay 复用 settings api.cancelJob/replayJob；
 * - 管理侧用户/部门只读文档下钻使用 admin 读端点；公共库与文档写操作复用 settings api；
 * - §8.1 summary 与 §8.4–8.5 投稿审核读模型复用 settings/types（同一批端点）。
 */

import type { ApiClient, AuthSessionGuard } from '../api/client';
import type {
  ApprovalDecisionResponse,
  ApprovalListResponse,
  ApprovalSummary,
  DocumentListResponse,
} from '../settings/types';
import type {
  AdminDepartmentItem,
  AdminDepartmentListResponse,
  AdminUserCreateInput,
  AdminUserDeleteResponse,
  AdminUserItem,
  AdminUserListQuery,
  AdminUserListResponse,
  AdminUserPatchInput,
  CalibrationWindow,
  CalibrationWindowAction,
  CalibrationWindowKind,
  DashboardResponse,
  DepartmentStatusFilter,
  GraphBuildCancelResponse,
  GraphBuildCurrent,
  GraphBuildRun,
  LeaderboardResponse,
  MetricsWindow,
  OperationsMetricsResponse,
  OpsJobsResponse,
  OpsJobsView,
  PermissionMatrixResponse,
  QuotaApproveResponse,
  QuotaRejectResponse,
  QuotaRequestListResponse,
  QuotaRequestStatus,
} from './types';

export interface AdminApi {
  /* ---------- §9 指标 ---------- */
  getDashboard(window: MetricsWindow, expand?: 'user_rank'): Promise<DashboardResponse>;
  getOperationsMetrics(window: MetricsWindow): Promise<OperationsMetricsResponse>;

  /* ---------- §12.6 管理侧只读文档下钻 ---------- */
  listUserDocuments(userId: string, page: number, pageSize: number): Promise<DocumentListResponse>;
  listDepartmentDocuments(
    departmentId: string,
    page: number,
    pageSize: number,
  ): Promise<DocumentListResponse>;

  /* ---------- §8 审批与投稿审核 ---------- */
  getApprovalSummary(): Promise<ApprovalSummary>;
  listQuotaRequests(status?: QuotaRequestStatus): Promise<QuotaRequestListResponse>;
  /** 批准：approvedPages 缺省（null）等于 requested_pages；携带 expected_version + Idempotency-Key。 */
  approveQuotaRequest(
    requestId: string,
    expectedVersion: number,
    approvedPages: number | null,
    idempotencyKey: string,
  ): Promise<QuotaApproveResponse>;
  /** 驳回：不接受说明字段；携带 expected_version + Idempotency-Key。 */
  rejectQuotaRequest(
    requestId: string,
    expectedVersion: number,
    idempotencyKey: string,
  ): Promise<QuotaRejectResponse>;
  /** §8.4：后端根据当前审核者范围返回待审投稿。 */
  listApprovalSubmissions(): Promise<ApprovalListResponse>;
  approveSubmission(
    submissionId: string,
    expectedVersion: number,
    idempotencyKey: string,
  ): Promise<ApprovalDecisionResponse>;
  rejectSubmission(
    submissionId: string,
    expectedVersion: number,
    reason: string | null,
    idempotencyKey: string,
  ): Promise<ApprovalDecisionResponse>;

  /* ---------- §6.12 公共库图谱维护（仅 ops） ---------- */
  getCurrentGraphBuild(): Promise<GraphBuildCurrent>;
  /** 202：返回 run 读模型（同 latest_run 形状）。 */
  createGraphBuild(expectedSourceRevision: number, idempotencyKey: string): Promise<GraphBuildRun>;
  cancelGraphBuild(
    graphBuildId: string,
    expectedVersion: number,
    idempotencyKey: string,
  ): Promise<GraphBuildCancelResponse>;

  /* ---------- §10 任务队列（行操作复用 settings api.cancelJob/replayJob） ---------- */
  listOpsJobs(view: OpsJobsView): Promise<OpsJobsResponse>;

  /* ---------- §11 评测与校准 ---------- */
  getLeaderboard(): Promise<LeaderboardResponse>;
  getCalibrationWindow(): Promise<CalibrationWindow>;
  /** 仅运维；open 必填 window_kind；携带 Idempotency-Key。 */
  postCalibrationWindow(
    action: CalibrationWindowAction,
    windowKind: CalibrationWindowKind | null,
    idempotencyKey: string,
  ): Promise<CalibrationWindow>;

  /* ---------- §12.1–12.4 用户管理（写操作不带 Idempotency-Key，仅 expected_version） ---------- */
  listUsers(query: AdminUserListQuery): Promise<AdminUserListResponse>;
  createUser(input: AdminUserCreateInput): Promise<AdminUserItem>;
  patchUser(userId: string, input: AdminUserPatchInput): Promise<AdminUserItem>;
  deleteUser(userId: string, expectedVersion: number): Promise<AdminUserDeleteResponse>;

  /* ---------- §12.5 部门目录与部门管理 ---------- */
  listDepartments(status?: DepartmentStatusFilter): Promise<AdminDepartmentListResponse>;
  createDepartment(name: string, idempotencyKey: string): Promise<AdminDepartmentItem>;
  renameDepartment(
    departmentId: string,
    expectedVersion: number,
    name: string,
    idempotencyKey: string,
  ): Promise<AdminDepartmentItem>;
  deactivateDepartment(
    departmentId: string,
    expectedVersion: number,
    idempotencyKey: string,
  ): Promise<AdminDepartmentItem>;

  /* ---------- §12.7 权限矩阵（超管只读） ---------- */
  getPermissionMatrix(): Promise<PermissionMatrixResponse>;
}

function idempotencyHeaders(idempotencyKey: string): Record<string, string> {
  return { 'Idempotency-Key': idempotencyKey };
}

export function createAdminApi(client: ApiClient): AdminApi {
  /** 全部请求（含读）绑定发起时的逻辑会话；响应前后 client 校验 fail-closed。 */
  function guard(): AuthSessionGuard {
    return client.captureAuthSessionGuard();
  }

  return {
    getDashboard(window, expand) {
      const authSessionGuard = guard();
      const query = new URLSearchParams({ window });
      if (expand !== undefined) {
        query.set('expand', expand);
      }
      return client.request<DashboardResponse>(`/metrics/dashboard?${query.toString()}`, {
        authSessionGuard,
      });
    },

    getOperationsMetrics(window) {
      const authSessionGuard = guard();
      return client.request<OperationsMetricsResponse>(`/metrics/operations?window=${window}`, {
        authSessionGuard,
      });
    },

    listUserDocuments(userId, page, pageSize) {
      const authSessionGuard = guard();
      const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
      return client.request<DocumentListResponse>(
        `/admin/users/${encodeURIComponent(userId)}/documents?${query.toString()}`,
        { authSessionGuard },
      );
    },

    listDepartmentDocuments(departmentId, page, pageSize) {
      const authSessionGuard = guard();
      const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
      return client.request<DocumentListResponse>(
        `/admin/departments/${encodeURIComponent(departmentId)}/documents?${query.toString()}`,
        { authSessionGuard },
      );
    },

    getApprovalSummary() {
      const authSessionGuard = guard();
      return client.request<ApprovalSummary>('/approvals/summary', { authSessionGuard });
    },

    listQuotaRequests(status) {
      const authSessionGuard = guard();
      const query = status === undefined ? '' : `?status=${status}`;
      return client.request<QuotaRequestListResponse>(`/approvals/quota-requests${query}`, {
        authSessionGuard,
      });
    },

    approveQuotaRequest(requestId, expectedVersion, approvedPages, idempotencyKey) {
      const authSessionGuard = guard();
      return client.request<QuotaApproveResponse>(
        `/approvals/quota-requests/${encodeURIComponent(requestId)}/approve`,
        {
          method: 'POST',
          body:
            approvedPages === null
              ? { expected_version: expectedVersion }
              : { expected_version: expectedVersion, approved_pages: approvedPages },
          headers: idempotencyHeaders(idempotencyKey),
          authSessionGuard,
        },
      );
    },

    rejectQuotaRequest(requestId, expectedVersion, idempotencyKey) {
      const authSessionGuard = guard();
      return client.request<QuotaRejectResponse>(
        `/approvals/quota-requests/${encodeURIComponent(requestId)}/reject`,
        {
          method: 'POST',
          body: { expected_version: expectedVersion },
          headers: idempotencyHeaders(idempotencyKey),
          authSessionGuard,
        },
      );
    },

    listApprovalSubmissions() {
      const authSessionGuard = guard();
      return client.request<ApprovalListResponse>('/approvals/submissions', {
        authSessionGuard,
      });
    },

    approveSubmission(submissionId, expectedVersion, idempotencyKey) {
      const authSessionGuard = guard();
      return client.request<ApprovalDecisionResponse>(
        `/approvals/submissions/${encodeURIComponent(submissionId)}/approve`,
        {
          method: 'POST',
          body: { expected_version: expectedVersion },
          headers: idempotencyHeaders(idempotencyKey),
          authSessionGuard,
        },
      );
    },

    rejectSubmission(submissionId, expectedVersion, reason, idempotencyKey) {
      const authSessionGuard = guard();
      return client.request<ApprovalDecisionResponse>(
        `/approvals/submissions/${encodeURIComponent(submissionId)}/reject`,
        {
          method: 'POST',
          body:
            reason === null
              ? { expected_version: expectedVersion }
              : { expected_version: expectedVersion, reason },
          headers: idempotencyHeaders(idempotencyKey),
          authSessionGuard,
        },
      );
    },

    getCurrentGraphBuild() {
      const authSessionGuard = guard();
      return client.request<GraphBuildCurrent>('/ops/graph-builds/current', { authSessionGuard });
    },

    createGraphBuild(expectedSourceRevision, idempotencyKey) {
      const authSessionGuard = guard();
      return client.request<GraphBuildRun>('/ops/graph-builds', {
        method: 'POST',
        body: { expected_source_revision: expectedSourceRevision },
        headers: idempotencyHeaders(idempotencyKey),
        authSessionGuard,
      });
    },

    cancelGraphBuild(graphBuildId, expectedVersion, idempotencyKey) {
      const authSessionGuard = guard();
      return client.request<GraphBuildCancelResponse>(
        `/ops/graph-builds/${encodeURIComponent(graphBuildId)}/cancel`,
        {
          method: 'POST',
          body: { expected_version: expectedVersion },
          headers: idempotencyHeaders(idempotencyKey),
          authSessionGuard,
        },
      );
    },

    listOpsJobs(view) {
      const authSessionGuard = guard();
      return client.request<OpsJobsResponse>(`/ops/jobs?view=${view}`, { authSessionGuard });
    },

    getLeaderboard() {
      const authSessionGuard = guard();
      return client.request<LeaderboardResponse>('/evaluation/leaderboard', { authSessionGuard });
    },

    getCalibrationWindow() {
      const authSessionGuard = guard();
      return client.request<CalibrationWindow>('/calibration/window', { authSessionGuard });
    },

    postCalibrationWindow(action, windowKind, idempotencyKey) {
      const authSessionGuard = guard();
      return client.request<CalibrationWindow>('/calibration/window', {
        method: 'POST',
        body:
          action === 'open' ? { action, window_kind: windowKind } : { action },
        headers: idempotencyHeaders(idempotencyKey),
        authSessionGuard,
      });
    },

    listUsers(query) {
      const authSessionGuard = guard();
      const params = new URLSearchParams();
      if (query.q !== undefined) params.set('q', query.q);
      if (query.departmentId !== undefined) params.set('department_id', query.departmentId);
      if (query.role !== undefined) params.set('role', query.role);
      if (query.page !== undefined) params.set('page', String(query.page));
      if (query.pageSize !== undefined) params.set('page_size', String(query.pageSize));
      const suffix = params.toString() === '' ? '' : `?${params.toString()}`;
      return client.request<AdminUserListResponse>(`/admin/users${suffix}`, { authSessionGuard });
    },

    createUser(input) {
      const authSessionGuard = guard();
      return client.request<AdminUserItem>('/admin/users', {
        method: 'POST',
        body: input,
        authSessionGuard,
      });
    },

    patchUser(userId, input) {
      const authSessionGuard = guard();
      return client.request<AdminUserItem>(`/admin/users/${encodeURIComponent(userId)}`, {
        method: 'PATCH',
        body: input,
        authSessionGuard,
      });
    },

    deleteUser(userId, expectedVersion) {
      const authSessionGuard = guard();
      return client.request<AdminUserDeleteResponse>(
        `/admin/users/${encodeURIComponent(userId)}`,
        {
          method: 'DELETE',
          body: { expected_version: expectedVersion },
          authSessionGuard,
        },
      );
    },

    listDepartments(status) {
      const authSessionGuard = guard();
      const query = status === undefined ? '' : `?status=${status}`;
      return client.request<AdminDepartmentListResponse>(`/admin/departments${query}`, {
        authSessionGuard,
      });
    },

    createDepartment(name, idempotencyKey) {
      const authSessionGuard = guard();
      return client.request<AdminDepartmentItem>('/admin/departments', {
        method: 'POST',
        body: { name },
        headers: idempotencyHeaders(idempotencyKey),
        authSessionGuard,
      });
    },

    renameDepartment(departmentId, expectedVersion, name, idempotencyKey) {
      const authSessionGuard = guard();
      return client.request<AdminDepartmentItem>(
        `/admin/departments/${encodeURIComponent(departmentId)}`,
        {
          method: 'PATCH',
          body: { expected_version: expectedVersion, name },
          headers: idempotencyHeaders(idempotencyKey),
          authSessionGuard,
        },
      );
    },

    deactivateDepartment(departmentId, expectedVersion, idempotencyKey) {
      const authSessionGuard = guard();
      return client.request<AdminDepartmentItem>(
        `/admin/departments/${encodeURIComponent(departmentId)}/deactivate`,
        {
          method: 'POST',
          body: { expected_version: expectedVersion },
          headers: idempotencyHeaders(idempotencyKey),
          authSessionGuard,
        },
      );
    },

    getPermissionMatrix() {
      const authSessionGuard = guard();
      return client.request<PermissionMatrixResponse>('/admin/permission-matrix', {
        authSessionGuard,
      });
    },
  };
}
