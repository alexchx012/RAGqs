/*
 * 设置域 API 封装（契约 §2.4–2.9、§6.2–6.11、§7.1–7.2、§8.1/8.4–8.5）。
 * 经 ApiClient 携带 /v1 前缀与 Bearer；multipart 不强制 Content-Type（由浏览器设边界）；
 * 写操作的 Idempotency-Key 由调用方生成并传入，本层原样透传。
 * 安全（review A1）：全部请求（含读）在发起时 capture 当前逻辑会话 authSessionGuard，
 * 响应前后由 client fail-closed 校验——logout/login 后旧请求不得重放或写入新用户。
 * 本文件只提供可调用封装与稳定返回值，不含 UI 状态。
 */

import type { ApiClient, AuthSessionGuard } from '../api/client';
import type {
  ApprovalDecisionResponse,
  ApprovalListResponse,
  ApprovalSummary,
  DocumentListQuery,
  DocumentListResponse,
  DocumentVersionsResponse,
  JobListQuery,
  JobListResponse,
  NewVersionResponse,
  QuotaRequestResult,
  QuotaSnapshot,
  ReplayJobResponse,
  RebuildDocumentResponse,
  RestoreVersionResponse,
  SpacesResponse,
  SubmissionContentResponse,
  SubmissionListResponse,
  SubmissionStatus,
  UploadBatch,
  UploadResponse,
  UserPreferences,
  UserProfile,
  WithdrawSubmissionResponse,
} from './types';

export interface SettingsApi {
  updateProfile(input: { display_name: string }): Promise<UserProfile>;
  uploadAvatar(file: File): Promise<{ avatar_url: string }>;
  changePassword(input: { old_password: string; new_password: string }): Promise<void>;
  getPreferences(): Promise<UserPreferences>;
  updatePreferences(input: UserPreferences): Promise<UserPreferences>;
  getQuota(): Promise<QuotaSnapshot>;
  requestQuota(pages: number, idempotencyKey: string): Promise<QuotaRequestResult>;
  listDocuments(input: DocumentListQuery): Promise<DocumentListResponse>;
  uploadDocuments(spaceId: string, files: readonly File[], idempotencyKey: string): Promise<UploadResponse>;
  /** §6.4 上传新版本：固定目标 document_id + expected_version，单文件 multipart。 */
  uploadNewVersion(
    documentId: string,
    file: File,
    expectedVersion: number,
    idempotencyKey: string,
  ): Promise<NewVersionResponse>;
  listJobs(input: JobListQuery): Promise<JobListResponse>;
  getUploadBatch(batchId: string): Promise<UploadBatch>;
  cancelJob(jobId: string, idempotencyKey: string): Promise<void>;
  replayJob(jobId: string, idempotencyKey: string): Promise<ReplayJobResponse>;
  ackNotification(eventId: string): Promise<void>;
  listVersions(documentId: string): Promise<DocumentVersionsResponse>;
  restoreVersion(
    documentId: string,
    versionId: string,
    expectedVersion: number,
    idempotencyKey: string,
  ): Promise<RestoreVersionResponse>;
  rebuildDocument(
    documentId: string,
    expectedVersion: number,
    idempotencyKey: string,
  ): Promise<RebuildDocumentResponse>;
  deleteDocument(documentId: string, expectedVersion: number, idempotencyKey: string): Promise<void>;
  listSubmissions(status: SubmissionStatus | 'all'): Promise<SubmissionListResponse>;
  getSubmissionContent(submissionId: string): Promise<SubmissionContentResponse>;
  withdrawSubmission(
    submissionId: string,
    expectedVersion: number,
    idempotencyKey: string,
  ): Promise<WithdrawSubmissionResponse>;
  deleteSubmission(submissionId: string, expectedVersion: number, idempotencyKey: string): Promise<void>;
  listManageSpaces(): Promise<SpacesResponse>;
  /** §6.1 上传目标空间（usage=upload）：manage 直接写入 / contribute 需审核。 */
  listUploadSpaces(): Promise<SpacesResponse>;
  getApprovalSummary(): Promise<ApprovalSummary>;
  listApprovals(): Promise<ApprovalListResponse>;
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
}

function idempotencyHeaders(idempotencyKey: string): Record<string, string> {
  return { 'Idempotency-Key': idempotencyKey };
}

export function createSettingsApi(client: ApiClient): SettingsApi {
  /** 全部请求（含读）绑定发起时的逻辑会话；响应前后 client 校验 fail-closed。 */
  function guard(): AuthSessionGuard {
    return client.captureAuthSessionGuard();
  }

  return {
    updateProfile(input) {
      const authSessionGuard = guard();
      return client.request<UserProfile>('/users/me/profile', {
        method: 'PATCH',
        body: input,
        authSessionGuard,
      });
    },

    uploadAvatar(file) {
      const authSessionGuard = guard();
      const form = new FormData();
      form.set('file', file);
      return client.request<{ avatar_url: string }>('/users/me/avatar', {
        method: 'POST',
        body: form,
        authSessionGuard,
      });
    },

    async changePassword(input) {
      const authSessionGuard = guard();
      await client.request<void>('/users/me/password', {
        method: 'PUT',
        body: input,
        authSessionGuard,
      });
    },

    getPreferences() {
      const authSessionGuard = guard();
      return client.request<UserPreferences>('/users/me/preferences', { authSessionGuard });
    },

    updatePreferences(input) {
      const authSessionGuard = guard();
      return client.request<UserPreferences>('/users/me/preferences', {
        method: 'PUT',
        body: input,
        authSessionGuard,
      });
    },

    getQuota() {
      const authSessionGuard = guard();
      return client.request<QuotaSnapshot>('/quota/me', { authSessionGuard });
    },

    requestQuota(pages, idempotencyKey) {
      const authSessionGuard = guard();
      return client.request<QuotaRequestResult>('/quota-requests', {
        method: 'POST',
        body: { requested_pages: pages },
        headers: idempotencyHeaders(idempotencyKey),
        authSessionGuard,
      });
    },

    listDocuments(input) {
      const authSessionGuard = guard();
      const params = new URLSearchParams();
      if (input.q !== undefined) params.set('q', input.q);
      if (input.page !== undefined) params.set('page', String(input.page));
      if (input.pageSize !== undefined) params.set('page_size', String(input.pageSize));
      const query = params.toString();
      const suffix = query === '' ? '' : `?${query}`;
      return client.request<DocumentListResponse>(
        `/spaces/${encodeURIComponent(input.spaceId)}/documents${suffix}`,
        { authSessionGuard },
      );
    },

    /**
     * 手工构建 multipart（files 字段，逐文件真实文件名）。
     * jsdom FormData 经 undici 序列化时把 File 名抹成 'blob'（浏览器真实 fetch 也由 UA 决定
     * 文件名呈现），mock 服务端需要按文件名返回逐文件结果，因此统一用字节级 multipart：
     * Uint8Array 在此栈（jsdom + undici + MSW）与浏览器 fetch 都原样透传，边界与文件名稳定。
     */
    async uploadDocuments(spaceId, files, idempotencyKey) {
      const authSessionGuard = guard();
      const boundary = `----RAGqsBoundary${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
      const encoder = new TextEncoder();
      const chunks: Uint8Array[] = [];
      for (const file of files) {
        const safeName = file.name.replace(/[\r\n"]/g, '_');
        chunks.push(
          encoder.encode(
            `--${boundary}\r\n` +
              `Content-Disposition: form-data; name="files"; filename="${safeName}"\r\n` +
              `Content-Type: ${file.type || 'application/octet-stream'}\r\n\r\n`,
          ),
        );
        const bytes = new Uint8Array(await file.arrayBuffer());
        chunks.push(bytes);
        chunks.push(encoder.encode('\r\n'));
      }
      chunks.push(encoder.encode(`--${boundary}--\r\n`));
      const length = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
      const body = new Uint8Array(length);
      let offset = 0;
      for (const chunk of chunks) {
        body.set(chunk, offset);
        offset += chunk.length;
      }
      return client.request<UploadResponse>(`/spaces/${encodeURIComponent(spaceId)}/documents`, {
        method: 'POST',
        body,
        headers: {
          'Content-Type': `multipart/form-data; boundary=${boundary}`,
          ...idempotencyHeaders(idempotencyKey),
        },
        authSessionGuard,
      });
    },

    /**
     * §6.4 上传新版本：固定目标 document_id（不进入目标选择），单文件，携带 expected_version。
     * 与 uploadDocuments 相同的字节级 multipart wire 格式（真实文件名 + 显式 boundary）。
     */
    async uploadNewVersion(documentId, file, expectedVersion, idempotencyKey) {
      const authSessionGuard = guard();
      const boundary = `----RAGqsBoundary${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
      const encoder = new TextEncoder();
      const safeName = file.name.replace(/[\r\n"]/g, '_');
      const chunks: Uint8Array[] = [
        encoder.encode(
          `--${boundary}\r\n` +
            `Content-Disposition: form-data; name="files"; filename="${safeName}"\r\n` +
            `Content-Type: ${file.type || 'application/octet-stream'}\r\n\r\n`,
        ),
        new Uint8Array(await file.arrayBuffer()),
        encoder.encode('\r\n'),
        encoder.encode(
          `--${boundary}\r\nContent-Disposition: form-data; name="expected_version"\r\n\r\n${expectedVersion}\r\n`,
        ),
        encoder.encode(`--${boundary}--\r\n`),
      ];
      const length = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
      const body = new Uint8Array(length);
      let offset = 0;
      for (const chunk of chunks) {
        body.set(chunk, offset);
        offset += chunk.length;
      }
      return client.request<NewVersionResponse>(
        `/documents/${encodeURIComponent(documentId)}/versions`,
        {
          method: 'POST',
          body,
          headers: {
            'Content-Type': `multipart/form-data; boundary=${boundary}`,
            ...idempotencyHeaders(idempotencyKey),
          },
          authSessionGuard,
        },
      );
    },

    listJobs(input) {
      const authSessionGuard = guard();
      const query = input.limit === undefined ? '' : `?limit=${input.limit}`;
      return client.request<JobListResponse>(`/ingestion-jobs${query}`, { authSessionGuard });
    },

    getUploadBatch(batchId) {
      const authSessionGuard = guard();
      return client.request<UploadBatch>(`/upload-batches/${encodeURIComponent(batchId)}`, {
        authSessionGuard,
      });
    },

    async cancelJob(jobId, idempotencyKey) {
      const authSessionGuard = guard();
      await client.request<void>(`/ingestion-jobs/${encodeURIComponent(jobId)}/cancel`, {
        method: 'POST',
        headers: idempotencyHeaders(idempotencyKey),
        authSessionGuard,
      });
    },

    replayJob(jobId, idempotencyKey) {
      const authSessionGuard = guard();
      return client.request<ReplayJobResponse>(
        `/ingestion-jobs/${encodeURIComponent(jobId)}/replay`,
        {
          method: 'POST',
          headers: idempotencyHeaders(idempotencyKey),
          authSessionGuard,
        },
      );
    },

    async ackNotification(eventId) {
      const authSessionGuard = guard();
      await client.request<void>(`/notifications/events/${encodeURIComponent(eventId)}/ack`, {
        method: 'POST',
        authSessionGuard,
      });
    },

    listVersions(documentId) {
      const authSessionGuard = guard();
      return client.request<DocumentVersionsResponse>(
        `/documents/${encodeURIComponent(documentId)}/versions`,
        { authSessionGuard },
      );
    },

    restoreVersion(documentId, versionId, expectedVersion, idempotencyKey) {
      const authSessionGuard = guard();
      return client.request<RestoreVersionResponse>(
        `/documents/${encodeURIComponent(documentId)}/versions/${encodeURIComponent(versionId)}/restore`,
        {
          method: 'POST',
          body: { expected_version: expectedVersion },
          headers: idempotencyHeaders(idempotencyKey),
          authSessionGuard,
        },
      );
    },

    rebuildDocument(documentId, expectedVersion, idempotencyKey) {
      const authSessionGuard = guard();
      return client.request<RebuildDocumentResponse>(
        `/documents/${encodeURIComponent(documentId)}/reindex`,
        {
          method: 'POST',
          body: { expected_version: expectedVersion },
          headers: idempotencyHeaders(idempotencyKey),
          authSessionGuard,
        },
      );
    },

    async deleteDocument(documentId, expectedVersion, idempotencyKey) {
      const authSessionGuard = guard();
      await client.request<void>(`/documents/${encodeURIComponent(documentId)}`, {
        method: 'DELETE',
        body: { expected_version: expectedVersion },
        headers: idempotencyHeaders(idempotencyKey),
        authSessionGuard,
      });
    },

    listSubmissions(status) {
      const authSessionGuard = guard();
      const query = status === 'all' ? '' : `?status=${encodeURIComponent(status)}`;
      return client.request<SubmissionListResponse>(`/submissions${query}`, { authSessionGuard });
    },

    getSubmissionContent(submissionId) {
      const authSessionGuard = guard();
      return client.request(`/submissions/${encodeURIComponent(submissionId)}/content`, {
        responseType: 'blob',
        authSessionGuard,
      });
    },

    withdrawSubmission(submissionId, expectedVersion, idempotencyKey) {
      const authSessionGuard = guard();
      return client.request<WithdrawSubmissionResponse>(
        `/submissions/${encodeURIComponent(submissionId)}/withdraw`,
        {
          method: 'POST',
          body: { expected_version: expectedVersion },
          headers: idempotencyHeaders(idempotencyKey),
          authSessionGuard,
        },
      );
    },

    async deleteSubmission(submissionId, expectedVersion, idempotencyKey) {
      const authSessionGuard = guard();
      await client.request<void>(`/submissions/${encodeURIComponent(submissionId)}`, {
        method: 'DELETE',
        body: { expected_version: expectedVersion },
        headers: idempotencyHeaders(idempotencyKey),
        authSessionGuard,
      });
    },

    listManageSpaces() {
      const authSessionGuard = guard();
      return client.request<SpacesResponse>('/spaces?usage=manage', { authSessionGuard });
    },

    listUploadSpaces() {
      const authSessionGuard = guard();
      return client.request<SpacesResponse>('/spaces?usage=upload', { authSessionGuard });
    },

    getApprovalSummary() {
      const authSessionGuard = guard();
      return client.request<ApprovalSummary>('/approvals/summary', { authSessionGuard });
    },

    listApprovals() {
      const authSessionGuard = guard();
      return client.request<ApprovalListResponse>('/approvals/submissions', { authSessionGuard });
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
          body: reason === null ? { expected_version: expectedVersion } : { expected_version: expectedVersion, reason },
          headers: idempotencyHeaders(idempotencyKey),
          authSessionGuard,
        },
      );
    },
  };
}
