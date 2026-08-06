/*
 * 站内提醒类型（契约《前端接口需求.md》§5.1、§13）。
 * type 为后端下发枚举：九类已知值之外允许未知值（§1 未知枚举兜底：保留条目、
 * 后端 title + 通用图标展示，不展示机读值、不崩溃）。
 * event_occurred_at 为业务事件实际发生时间（ISO 8601 UTC），用于相对时间展示。
 */

export type KnownNotificationType =
  | 'ingestion_completed'
  | 'ocr_low_confidence'
  | 'quota_approved'
  | 'quota_rejected'
  | 'submission_approved'
  | 'submission_rejected'
  | 'submission_invalidated'
  | 'calibration_window_suggested'
  | 'graph_build_completed';

/** type 以后端下发为准，未知值兜底为 string。 */
export type NotificationType = KnownNotificationType | (string & {});

export interface IngestionPayload {
  readonly job_id: string;
  readonly document_id: string;
}

export interface QuotaPayload {
  readonly request_id: string;
}

export interface SubmissionPayload {
  readonly submission_id: string;
  readonly document_id?: string;
  readonly job_id?: string;
  readonly reason?: string;
}

export interface GraphBuildPayload {
  readonly graph_build_id: string;
  readonly status: 'succeeded' | 'failed' | 'cancelled';
  readonly source_revision?: string;
  readonly graph_generation_id?: string;
  readonly failure_class?: string;
}

/** payload 携带跳转所需 ID（§13）；未知类型保持后端原样对象。 */
export type NotificationPayload =
  | IngestionPayload
  | QuotaPayload
  | SubmissionPayload
  | GraphBuildPayload
  | Record<string, unknown>;

export interface NotificationItem {
  readonly id: string;
  readonly type: NotificationType;
  /** 后端一句话描述；文档永久删除后为后端固定脱敏文案，前端原样展示不恢复。 */
  readonly title: string;
  readonly payload: NotificationPayload;
  /** 权威已读状态（§5.3）；前端不自行持久化第二份。 */
  readonly read: boolean;
  readonly event_occurred_at: string;
}
