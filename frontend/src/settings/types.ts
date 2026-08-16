/*
 * 设置域读模型类型（契约《前端接口需求.md》§2.4–2.9、§6.2–6.11、§7、§8.1/8.4–8.5）。
 * 仅描述 API 稳定返回值；不含 UI 状态。后续账户/外观/知识库模块共用本文件。
 */

import type { User } from '../auth/types';

/** §2.4 个人资料：与认证域 User 同构。 */
export type UserProfile = User;

/** §2.9 外观与隐私偏好。 */
export type ThemePreferenceValue = 'light' | 'dark' | 'system';
export type ChatFontSize = 'standard' | 'large';

export interface UserPreferences {
  readonly theme: ThemePreferenceValue;
  readonly chat_font_size: ChatFontSize;
  readonly ab_opt_out: boolean;
}

/** §7.1 配额计数器。 */
export interface QuotaPendingRequest {
  readonly id: string;
  readonly version: number;
  readonly requested_pages: number;
  readonly quota_period: string;
  readonly created_at: string;
}

export interface QuotaSnapshot {
  readonly used: number;
  readonly base_limit: number;
  readonly extra_granted: number;
  readonly effective_limit: number;
  readonly unlimited: boolean;
  readonly reset_at: string;
  readonly business_timezone: string;
  readonly quota_period: string;
  readonly business_calendar_version_id: string;
  readonly pending_request: QuotaPendingRequest | null;
}

/** §7.2 配额申请 201 响应。 */
export interface QuotaRequestResult {
  readonly id: string;
  readonly version: number;
  readonly status: 'pending';
  readonly requested_pages: number;
  readonly quota_period: string;
  readonly created_at: string;
}

/** §6.2 文档列表。 */
export interface DocumentUsage {
  readonly pages: number;
  readonly images: number;
}

export interface DocumentActiveOperation {
  readonly job_id: string;
  readonly operation: string;
  readonly state: string;
}

export interface DocumentListItem {
  readonly id: string;
  readonly document_version_id: string;
  readonly version: number;
  readonly name: string;
  readonly media_kind: string;
  readonly version_status: string;
  readonly active_operation: DocumentActiveOperation | null;
  readonly uploaded_at: string;
  readonly usage: DocumentUsage;
}

export interface DocumentListQuery {
  readonly spaceId: string;
  readonly q?: string;
  readonly page?: number;
  readonly pageSize?: number;
}

export interface DocumentListResponse {
  readonly items: readonly DocumentListItem[];
  readonly total: number;
  readonly page: number;
  readonly page_size: number;
}

/** §6.3 管理上传响应。 */
export interface ManageUploadItem {
  readonly filename: string;
  readonly document_id: string;
  readonly document_version_id: string | null;
  readonly job_id: string | null;
  readonly publication_id: string | null;
  readonly deduplicated: boolean;
  readonly status: string;
}

/** §6.10 投稿创建响应（contribute 空间上传）。 */
export interface SubmissionUploadItem {
  readonly submission_id: string;
  readonly version: number;
  readonly status: SubmissionStatus;
  readonly space_id: string;
  readonly quota_exempt: boolean;
  readonly document_id: null;
  readonly document_version_id: null;
  readonly job_id: null;
}

export type UploadItem = ManageUploadItem | SubmissionUploadItem;

export interface UploadResponse {
  readonly upload_batch_id?: string | null;
  readonly items: readonly UploadItem[];
}

/** §6.3.1 批次汇总。 */
export interface UploadBatchSummary {
  readonly total_files: number;
  readonly pending: number;
  readonly running: number;
  readonly retry_wait: number;
  readonly succeeded: number;
  readonly failed: number;
  readonly cancelled: number;
  readonly dead_letter: number;
  readonly rejected: number;
  readonly deduplicated: number;
}

export interface UploadBatch {
  readonly upload_batch_id: string;
  readonly state: string;
  readonly summary: UploadBatchSummary;
}

/** §6.6 入库任务。 */
export type JobAction = 'cancel' | 'replay';

/** replay 响应：客户端按 job_id + replay_generation 轮询收敛，不能一次 GET 后立即结束。 */
export interface ReplayJobResponse {
  readonly job_id: string;
  readonly state: string;
  readonly replay_generation: number;
}

export interface IngestionJob {
  readonly job_id: string;
  readonly document_id: string | null;
  readonly name: string;
  readonly space_id: string;
  readonly upload_batch_id: string | null;
  readonly state: string;
  readonly stage: string | null;
  readonly progress_text_hint: string | null;
  readonly next_attempt_at: string | null;
  readonly usage: DocumentUsage | null;
  readonly failure_reason: string | null;
  readonly allowed_actions: readonly JobAction[];
  readonly ocr_low_confidence: boolean;
  readonly notification_event_ids: readonly string[];
  readonly created_at: string;
}

export interface JobListQuery {
  readonly limit?: number;
}

export interface JobListResponse {
  readonly items: readonly IngestionJob[];
  readonly limit: number;
  readonly max_limit: number;
  readonly has_more: boolean;
}

/** §6.9 版本记录。 */
export interface DocumentVersionItem {
  readonly document_version_id: string;
  readonly version_number: number;
  readonly status: string;
  readonly created_at: string;
  readonly activated_at: string | null;
  readonly terminal_at: string | null;
  readonly superseded_at: string | null;
  readonly purge_after_at: string | null;
  readonly purged_at: string | null;
  readonly restored_from_version_id: string | null;
  readonly content_available: boolean;
}

export interface DocumentVersionsResponse {
  readonly document_id: string;
  readonly version: number;
  readonly active_version_id: string | null;
  readonly items: readonly DocumentVersionItem[];
}

export interface RestoreVersionResponse {
  readonly document_id: string;
  readonly document_version_id: string;
  readonly restored_from_version_id: string;
  readonly job_id: string;
  readonly version: number;
}

export interface RebuildDocumentResponse {
  readonly document_id: string;
  readonly document_version_id: string;
  readonly job_id: string;
  readonly version: number;
}

/** §6.4 上传新版本：固定目标（document_id）+ expected_version，任务进上传结果层。 */
interface NewVersionResponseBase {
  readonly document_id: string;
  readonly document_version_id: string;
  readonly version: number;
}

export type NewVersionResponse =
  | (NewVersionResponseBase & {
      readonly job_id: null;
      readonly deduplicated: true;
      readonly status: 'active';
    })
  | (NewVersionResponseBase & {
      readonly job_id: string;
      readonly publication_id: string;
      readonly deduplicated: false;
      readonly status: 'pending';
    });

/** §6.10 投稿。 */
export type SubmissionStatus = 'pending' | 'approved' | 'rejected' | 'withdrawn' | 'invalidated';

/** 撤回响应：服务端只返回 { submission_id, version, status }；前端与原行合并，不覆盖文件名/空间/时间。 */
export interface WithdrawSubmissionResponse {
  readonly submission_id: string;
  readonly version: number;
  readonly status: 'withdrawn';
}

export interface Submission {
  readonly submission_id: string;
  readonly space_id: string;
  readonly version: number;
  readonly status: SubmissionStatus;
  readonly file_name: string;
  readonly media_kind: string;
  readonly created_at: string;
  readonly reviewed_at: string | null;
}

export interface SubmissionListResponse {
  readonly items: readonly Submission[];
}

/** content 端点返回受控文件流；调用方自行处理 blob/URL。此处仅占位类型。 */
export type SubmissionContentResponse = Blob;

/** §6.1 空间列表（manage 用途，部长部门库入口）。 */
export type SpaceKind = 'personal' | 'department' | 'public';
export type SpacePermission = 'manage' | 'read' | 'contribute';

export interface SpaceItem {
  readonly id: string;
  readonly kind: SpaceKind;
  readonly name: string;
  readonly permission: SpacePermission;
  readonly document_count: number;
  readonly department_status?: 'active' | 'inactive';
}

export interface SpacesResponse {
  readonly items: readonly SpaceItem[];
}

/** §8.1 / §8.4–8.5 审核。 */
export interface ApprovalSummary {
  readonly quota_pending: number;
  readonly submission_pending: number;
}

export interface ApprovalListItem {
  readonly submission_id: string;
  readonly space_id: string;
  readonly version: number;
  readonly status: SubmissionStatus;
  readonly file_name: string;
  readonly media_kind: string;
  readonly created_at: string;
  readonly reviewed_at: string | null;
}

export interface ApprovalListResponse {
  readonly items: readonly ApprovalListItem[];
}

export interface ApprovalDecisionResponse {
  readonly submission_id: string;
  readonly version: number;
  readonly status: 'approved' | 'rejected';
  readonly document_id?: string;
  readonly document_version_id?: string;
  readonly job_id?: string;
}
