/*
 * 管理面板域契约类型（契约《前端接口需求.md》§6.12、§8.1–8.5、§9.1–9.2、
 * §10.1–10.2、§11.1–11.3、§12.1–12.7；后端设计 §2.6.1、§9.2）。
 * 仅描述 API 稳定返回值与写操作请求体；不含 UI 状态。
 * §8.1/§8.4–8.5 投稿审核与设置域共用同一批端点，读模型直接复用 settings/types，
 * 不在此重复定义（单一事实源）；角色枚举复用 auth/types 的 Role。
 */

import type { DepartmentRef, Role } from '../auth/types';
import type { JobAction } from '../settings/types';

export type {
  ApprovalDecisionResponse,
  ApprovalListItem,
  ApprovalListResponse,
  ApprovalSummary,
} from '../settings/types';

/* ---------- §9.1 / §9.2 指标（dashboard 与指标看板） ---------- */

export type MetricsWindow = 'today' | '7d' | '30d';

/** dashboard 卡 link 键集合（运维端 §7.2 跳转目标；超管卡恒 null）。 */
export type DashboardLinkKey =
  | 'ops.jobs'
  | 'ops.metrics'
  | 'ops.approvals.quota'
  | 'ops.approvals.submissions'
  | 'ops.spaces.public';

export type MetricDeltaDirection = 'up' | 'down' | 'flat';

/** 与上一窗口对比；text_hint 为机读增量，展示措辞前端定。 */
export interface MetricDelta {
  readonly direction: MetricDeltaDirection;
  readonly text_hint: string;
}

/** 阈值数值与比较方向只由后端下发；below 用于缓存命中率类「低于下限为异常」。 */
export interface MetricThreshold {
  readonly value: number;
  readonly direction: 'above' | 'below';
}

interface DashboardCardBase {
  readonly key: string;
  readonly title: string;
  /** 仅运维包携带；超管包恒 null。 */
  readonly threshold: MetricThreshold | null;
  /** 运维卡整卡可点的跳转目标；超管卡恒 null（不可点击）。 */
  readonly link: DashboardLinkKey | null;
}

/** stat 卡：数值 + delta 行 + 无轴 sparkline；窗口内无数据时 value null + 空 sparkline。 */
export interface StatDashboardCard extends DashboardCardBase {
  readonly kind: 'stat';
  readonly value: number | null;
  readonly delta: MetricDelta | null;
  readonly sparkline: readonly number[];
}

export interface DistributionRow {
  readonly label: string;
  readonly value: number;
  readonly ratio: number;
  /** warning 行文字琥珀（OCR 低置信区间等）。 */
  readonly tone: 'normal' | 'warning';
}

/** distribution 卡：无轴横向条形，条宽按 ratio。 */
export interface DistributionDashboardCard extends DashboardCardBase {
  readonly kind: 'distribution';
  readonly rows: readonly DistributionRow[];
}

/** count 卡（待办卡）：无 sparkline；>0 数值旁计数徽标。 */
export interface CountDashboardCard extends DashboardCardBase {
  readonly kind: 'count';
  readonly value: number | null;
  readonly delta: MetricDelta | null;
}

export interface UserRankRow {
  readonly label: string;
  readonly value: number;
  readonly ratio: number;
}

/** user_rank 排行卡：响应 rows 直接给到上限（50），前端前 10 行 + 展开全部，不发额外请求。 */
export interface UserRankDashboardCard extends DashboardCardBase {
  readonly kind: 'user_rank';
  readonly rows: readonly UserRankRow[];
  readonly total_count: number;
}

export type DashboardCard =
  | StatDashboardCard
  | DistributionDashboardCard
  | CountDashboardCard
  | UserRankDashboardCard;

/** 后端按角色组包，前端不判断角色差异、按 packs/cards 数据驱动渲染。 */
export interface MetricsPack {
  readonly key: string;
  readonly title: string;
  /** 包标题下一行 15px slate 说明（超管四包携带；运维包不带，前端有则渲染）。 */
  readonly description?: string;
  readonly cards: readonly DashboardCard[];
}

export interface DashboardResponse {
  readonly window: MetricsWindow;
  readonly packs: readonly MetricsPack[];
}

/** §9.2 指标看板：卡结构同 §9.1，固定三张卡，无组包。 */
export interface OperationsMetricsResponse {
  readonly window: MetricsWindow;
  readonly cards: readonly DashboardCard[];
}

/* ---------- §8.2–8.3 配额申请审批（仅运维） ---------- */

export type QuotaRequestStatus = 'pending' | 'approved' | 'rejected' | 'cancelled';

export interface QuotaRequestApplicant {
  readonly id: string;
  readonly display_name: string;
}

export interface QuotaRequestUsage {
  readonly used: number;
  readonly effective_limit: number;
}

export interface QuotaRequestItem {
  readonly id: string;
  readonly version: number;
  readonly status: QuotaRequestStatus;
  readonly applicant: QuotaRequestApplicant;
  readonly current_usage: QuotaRequestUsage;
  readonly requested_pages: number;
  readonly approved_pages: number | null;
  readonly quota_period: string;
  readonly created_at: string;
  readonly reviewed_at: string | null;
}

export interface QuotaRequestListResponse {
  readonly items: readonly QuotaRequestItem[];
}

/** 批准成功 200：追加额度与申请状态同事务提交。 */
export interface QuotaApproveResponse {
  readonly id: string;
  readonly version: number;
  readonly status: 'approved';
  readonly approved_pages: number;
  readonly credit_entry_id: string;
  readonly quota_period: string;
}

/** 驳回成功 200：不生成额度分录。 */
export interface QuotaRejectResponse {
  readonly id: string;
  readonly version: number;
  readonly status: 'rejected';
}

/* ---------- §6.12 公共库图谱维护（仅 ops；后端设计 §2.6.1） ---------- */

export type GraphAvailability = 'disabled' | 'ready' | 'stale';

export type GraphBuildStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';

/** V1 的唯一动作值。 */
export type GraphBuildAction = 'cancel';

export interface GraphGeneration {
  readonly graph_generation_id: string;
  readonly source_revision: number;
  readonly built_at: string;
}

export interface GraphBuildUsage {
  readonly primary_model_calls: number;
  readonly provider_calls: number;
}

export interface GraphBuildRun {
  readonly graph_build_id: string;
  /** 从 1 开始并随每次状态转换递增。 */
  readonly version: number;
  readonly state: GraphBuildStatus;
  readonly source_revision: number;
  readonly estimated_primary_model_calls: number;
  readonly actual_usage: GraphBuildUsage | null;
  readonly created_at: string;
  readonly started_at: string | null;
  readonly completed_at: string | null;
  readonly failure_class: string | null;
  readonly allowed_actions: readonly GraphBuildAction[];
}

/** 公共库图谱状态投影（唯一 space_id=public）。 */
export interface GraphBuildCurrent {
  readonly space_id: 'public';
  readonly source_revision: number;
  readonly graph_availability: GraphAvailability;
  readonly active_generation: GraphGeneration | null;
  readonly latest_run: GraphBuildRun | null;
}

/** 取消成功 200（成功或重复取消返回当前终态）。 */
export interface GraphBuildCancelResponse {
  readonly graph_build_id: string;
  readonly version: number;
  readonly state: GraphBuildStatus;
}

/* ---------- §10.1 任务队列（运维操作 / 超管只读） ---------- */

export type OpsJobsView = 'all' | 'active' | 'replayable' | 'stale';

/** V1 只有 ingestion。 */
export type OpsTaskType = 'ingestion';

export type OpsJobState =
  | 'pending'
  | 'running'
  | 'retry_wait'
  | 'succeeded'
  | 'failed'
  | 'dead_letter'
  | 'cancelled';

export interface OpsJobItem {
  readonly job_id: string;
  readonly task_type: OpsTaskType;
  readonly document_name: string;
  readonly state: OpsJobState;
  /** 超时派生标记（running 超租约），不是额外状态。 */
  readonly stale: boolean;
  /** 与 §6.6 同一规则；超管行由服务端返回空数组。 */
  readonly allowed_actions: readonly JobAction[];
  readonly enqueued_at: string;
  readonly wait_seconds: number;
}

export interface OpsJobsResponse {
  readonly items: readonly OpsJobItem[];
  /** 左栏与下钻项琥珀计数徽标数据源（>0 显示）。 */
  readonly stale_count: number;
}

/* ---------- §11.1–11.3 评测与校准 ---------- */

export interface LeaderboardEntry {
  readonly rank: number;
  readonly name: string;
  readonly score: number;
  /** 键值对 map，前端动态渲染列。 */
  readonly metrics: Record<string, number>;
  readonly eligible: boolean;
  /** 当前生效配置行（行底 fog-white 标示）。 */
  readonly is_active: boolean;
}

/** 当前视图的唯一策略数值来源；采样率均以 0–1 小数表达。 */
export interface EvaluationPolicy {
  readonly policy_version: string;
  readonly min_real_queries: number;
  readonly shadow_max_examples: number;
  readonly shadow_max_candidate_configs: number;
  readonly calibration_open_score_gap: number;
  readonly cold_start_sample_rate: number;
  readonly sentinel_sample_rate: number;
}

export interface LeaderboardResponse {
  readonly entries: readonly LeaderboardEntry[];
  readonly shadow_entries: readonly LeaderboardEntry[];
  readonly policy: EvaluationPolicy;
}

export type CalibrationWindowStatus = 'open' | 'closing' | 'closed';

export type CalibrationWindowKind = 'cold_start' | 'sentinel' | 'manual';

export type CalibrationWindowAction = 'open' | 'close';

/**
 * 校准窗口读模型；无 open/closing 窗口时服务端返回合成 closed
 * （window_id / window_kind / policy_version / opened_at / closed_at 等全 null，
 * sample_rate 0、pairs_collected 0）。
 */
export interface CalibrationWindow {
  readonly window_id: string | null;
  readonly status: CalibrationWindowStatus;
  readonly opened_at: string | null;
  readonly closed_at: string | null;
  readonly pairs_collected: number;
  readonly close_deadline_at: string | null;
  readonly window_kind: CalibrationWindowKind | null;
  readonly policy_version: string | null;
  readonly sample_rate: number;
  readonly opened_by: string | null;
  readonly closed_by: string | null;
}

/* ---------- §12.1–12.4 用户管理 ---------- */

export type UserLifecycleStatus = 'active' | 'pending_delete' | 'deleted';

export interface AdminUserItem {
  readonly id: string;
  readonly username: string;
  readonly real_name: string;
  readonly display_name: string;
  readonly department: DepartmentRef | null;
  readonly role: Role;
  readonly last_active_at: string | null;
  /** 个人库文档数（下钻行摘要）。 */
  readonly document_count: number;
  readonly version: number;
  readonly lifecycle_status: UserLifecycleStatus;
  readonly deletion_requested_at: string | null;
  readonly purge_after_at: string | null;
}

export interface AdminUserListQuery {
  /** 聚合查找：单输入匹配姓名 / 显示名 / 用户名 / 部门名 / 角色名。 */
  readonly q?: string;
  readonly departmentId?: string;
  readonly role?: Role;
  readonly page?: number;
  readonly pageSize?: number;
}

export interface AdminUserListResponse {
  readonly items: readonly AdminUserItem[];
  readonly total: number;
  readonly page: number;
  readonly page_size: number;
}

export interface AdminUserCreateInput {
  readonly username: string;
  readonly real_name: string;
  /** 缺省 = real_name。 */
  readonly display_name?: string;
  /** null 表示无部门；非空值必须指向 active 部门。 */
  readonly department_id: string | null;
  readonly role: Role;
  readonly initial_password: string;
}

export interface AdminUserPatchInput {
  readonly expected_version: number;
  /** 未提交保持原值；按操作者权限集切换。 */
  readonly role?: Role;
  /** 未提交保持原值；显式 null 解除部门归属。 */
  readonly department_id?: string | null;
}

/** 永久禁用 202：账号立即永久冻结并撤销全部会话。 */
export interface AdminUserDeleteResponse {
  readonly id: string;
  readonly version: number;
  readonly lifecycle_status: 'pending_delete';
  readonly deletion_requested_at: string;
  readonly purge_after_at: string;
}

/* ---------- §12.5 部门目录与部门管理 ---------- */

export type DepartmentStatus = 'active' | 'inactive';

export type DepartmentStatusFilter = 'active' | 'inactive' | 'all';

/** 行操作唯一依据；未知值不渲染。 */
export type DepartmentAction = 'rename' | 'deactivate';

export interface AdminDepartmentItem {
  readonly id: string;
  readonly name: string;
  readonly status: DepartmentStatus;
  readonly version: number;
  readonly document_count: number;
  readonly member_count: number;
  readonly nonterminal_job_count: number;
  readonly pending_submission_count: number;
  readonly deactivated_at: string | null;
  /** 由服务端按当前角色、部门状态和请求时刻计算；前端不复制资格判断。 */
  readonly allowed_actions: readonly DepartmentAction[];
}

export interface AdminDepartmentListResponse {
  readonly items: readonly AdminDepartmentItem[];
}

/* ---------- §12.7 权限矩阵（超管只读） ---------- */

export interface PermissionMatrixRow {
  readonly key: string;
  readonly label: string;
  readonly roles: Record<Role, boolean>;
}

export interface PermissionMatrixResponse {
  readonly capabilities: readonly PermissionMatrixRow[];
}

/* ---------- 备份与恢复（backup-restore-operations-layer 规格 §2；严格 ops-only） ---------- */

export type OpsBackupStatus = 'creating' | 'complete' | 'failed';

export interface OpsBackupItem {
  readonly backup_id: string;
  readonly status: OpsBackupStatus;
  readonly created_at: string;
  readonly completed_at: string | null;
  /** 仅 complete 且未清理的备份可恢复（purged / creating / failed 均 false）。 */
  readonly restorable: boolean;
}

/** 备份集组成物（kind 与后端同一词表：postgres_snapshot / object_store_snapshot / object_manifest）。 */
export interface OpsBackupComponent {
  readonly kind: string;
  readonly status: OpsBackupStatus;
  readonly reference?: string | null;
  readonly failure_reason?: string | null;
}

export interface OpsBackupDetail extends OpsBackupItem {
  readonly components: readonly OpsBackupComponent[];
}

export interface OpsBackupListResponse {
  readonly items: readonly OpsBackupItem[];
  readonly page: number;
  readonly page_size: number;
  readonly total: number;
}

/** POST /ops/backups 202 受理响应。 */
export interface OpsBackupCreateResponse {
  readonly backup_id: string;
  readonly status: OpsBackupStatus;
}

/** 与内部状态机同词表：accepted/running/blocked 为 active（blocked = 有 open 修复目标），succeeded/failed 为终态。 */
export type OpsRestoreStatus = 'accepted' | 'running' | 'blocked' | 'succeeded' | 'failed';

export interface OpsRestoreItem {
  readonly restore_id: string;
  readonly backup_id: string;
  readonly status: OpsRestoreStatus;
  readonly created_at: string;
  readonly completed_at: string | null;
}

export interface OpsRestoreListResponse {
  readonly items: readonly OpsRestoreItem[];
  readonly page: number;
  readonly page_size: number;
  readonly total: number;
}

/** POST /ops/restores 202 受理响应。 */
export interface OpsRestoreCreateResponse {
  readonly restore_id: string;
  readonly backup_id: string;
  readonly status: OpsRestoreStatus;
}

export type OpsRestoreStageStatus = 'pending' | 'running' | 'succeeded' | 'failed';

/** stage 与后端固定阶段词表一致（postgres / object_store / milvus / sparse / summary / graph / cache）。 */
export interface OpsRestoreStage {
  readonly stage: string;
  readonly status: OpsRestoreStageStatus;
  readonly validated?: boolean;
}

/** repair queue 目标：open 可重试，succeeded 已修复。 */
export type OpsRepairTargetStatus = 'open' | 'succeeded';

export interface OpsRepairTarget {
  readonly target_id: string;
  readonly stage: string;
  readonly resource_id: string;
  readonly status: OpsRepairTargetStatus;
  readonly failure_classification: string;
  readonly attempts?: number;
}

export interface OpsRestoreDetail extends OpsRestoreItem {
  readonly failure_reason?: string | null;
  readonly stages: readonly OpsRestoreStage[];
  readonly repair_targets: readonly OpsRepairTarget[];
}

/** POST repair-target retry 202 受理响应（target 保持 open，attempts 递增）。 */
export interface OpsRepairTargetRetryResponse {
  readonly target_id: string;
  readonly status: OpsRepairTargetStatus;
}

export type BackupPolicyFrequency = 'daily' | 'weekly';

export interface BackupPolicy {
  readonly enabled: boolean;
  readonly frequency: BackupPolicyFrequency;
  /** HH:MM（策略时区本地时间）。 */
  readonly local_time: string;
  /** 仅 weekly 使用；星期取值 0=周一 … 6=周日（与后端 Python date.weekday() 一致），至少一天。 */
  readonly weekdays: readonly number[];
  /** IANA 时区名。 */
  readonly timezone: string;
  readonly keep_last: number;
  readonly retention_days: number;
  readonly version: number;
  readonly next_run_at: string | null;
  readonly last_scheduled_for?: string | null;
  readonly last_outcome?: string | null;
}

/** PATCH /ops/backup-policy 请求体：expected_version 必填 + 字段子集。 */
export interface BackupPolicyPatchInput {
  readonly expected_version: number;
  readonly enabled?: boolean;
  readonly frequency?: BackupPolicyFrequency;
  readonly local_time?: string;
  readonly weekdays?: readonly number[];
  readonly timezone?: string;
  readonly keep_last?: number;
  readonly retention_days?: number;
}
