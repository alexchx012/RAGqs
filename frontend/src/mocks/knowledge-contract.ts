/*
 * 知识库契约 mock 核心（契约《前端接口需求.md》§6.2–6.11、§8.1/8.4–8.5）。
 * 与传输层无关（knowledge-handlers.ts 负责 MSW 接线）。真实模拟：
 * - 文档列表：每账号个人库 + 部门库 + 公共库；q 按文件名过滤；page/page_size 分页；默认上传时间倒序；
 *   active_operation 非空表示该文档正被任务更新。
 * - 上传三分支：manage 直接写入（创建文档/版本 + 入库任务 + 批次）、contribute 创建投稿
 *   （不检查页额度）；逐文件结果按服务端错误对象；409 quota_exceeded 整批拒绝。
 * - 任务状态机：queued→parsing→indexing→succeeded（夹具推进）；failed/dead_letter 带
 *   failure_reason；retry_wait 带 next_attempt_at；allowed_actions 唯一依据；成功时登记
 *   notification_event_ids（ingestion_completed / ocr_low_confidence，均 ackable）。
 * - 版本记录：active / superseded / failed / cancelled / purging / purged；恢复创建新版本
 *   与新任务；410 document_version_purged。
 * - 投稿五态与 409 系列（version_conflict / submission_state_conflict）；查看内容 404
 *   submission_content_unavailable；审核通过/驳回后投稿人侧状态联动 + 铃铛送达。
 */

import type { User } from '../auth/types';
import type { OpsJobItem, OpsJobsView } from '../admin/types';
import type {
  ApprovalDecisionResponse,
  ApprovalListItem,
  ApprovalSummary,
  DocumentListItem,
  DocumentVersionItem,
  IngestionJob,
  JobAction,
  NewVersionResponse,
  RebuildDocumentResponse,
  RestoreVersionResponse,
  Submission,
  SubmissionStatus,
  UploadBatch,
  UploadItem,
  UploadResponse,
  WithdrawSubmissionResponse,
} from '../settings/types';
import { MockHttpError } from './auth-contract';
import { MockNotificationsController } from './notifications-contract';
import { MockQuotaStore } from './quota-contract';

export type KnowledgeAuth = (auth: string | null) => User;

/** 空间定义（与 chat-contract §6.1 同一组数据；本模块只消费上传/管理所需的字段）。 */
interface SpaceDef {
  readonly id: string;
  readonly kind: 'personal' | 'department' | 'public';
  readonly name: string;
  readonly ownerUserId?: string;
  readonly departmentId?: string;
  readonly isPublic?: boolean;
  /** 部门空间已停用（§8.4 超管审核范围为「全部 active 部门」，inactive 部门空间不在其列）。 */
  readonly inactive?: boolean;
}

const SPACE_DEFS: readonly SpaceDef[] = [
  { id: 'personal:u_user', kind: 'personal', name: '个人库', ownerUserId: 'u_user' },
  { id: 'personal:u_minister', kind: 'personal', name: '个人库', ownerUserId: 'u_minister' },
  { id: 'personal:u_ops', kind: 'personal', name: '个人库', ownerUserId: 'u_ops' },
  { id: 'personal:u_admin', kind: 'personal', name: '个人库', ownerUserId: 'u_admin' },
  // 管理端用户列表下钻（§7.3）：admin 种子用户均持有个人库，无文档种子的空间返回空列表
  { id: 'personal:u_chen', kind: 'personal', name: '个人库', ownerUserId: 'u_chen' },
  { id: 'personal:u_zhao', kind: 'personal', name: '个人库', ownerUserId: 'u_zhao' },
  { id: 'personal:u_sun', kind: 'personal', name: '个人库', ownerUserId: 'u_sun' },
  { id: 'personal:u_ghost', kind: 'personal', name: '个人库', ownerUserId: 'u_ghost' },
  { id: 'department:d_finance', kind: 'department', name: '财务部', departmentId: 'd_finance' },
  { id: 'department:d_hr', kind: 'department', name: '人事部', departmentId: 'd_hr' },
  // 管理端部门库下钻（§7.3）：d_empty 空列表、d_legacy 已停用（只读由 chat §6.1 权限推导）
  { id: 'department:d_empty', kind: 'department', name: '空壳部', departmentId: 'd_empty' },
  { id: 'department:d_legacy', kind: 'department', name: '档案部', departmentId: 'd_legacy', inactive: true },
  { id: 'public', kind: 'public', name: '公共库', isPublic: true },
];

interface StoredVersion {
  readonly documentVersionId: string;
  readonly versionNumber: number;
  status: 'active' | 'superseded' | 'failed' | 'cancelled' | 'purging' | 'purged' | 'processing';
  readonly createdAt: string;
  activatedAt: string | null;
  terminalAt: string | null;
  supersededAt: string | null;
  purgeAfterAt: string | null;
  purgedAt: string | null;
  restoredFromVersionId: string | null;
  contentAvailable: boolean;
  /** 处理中版本：对应的 restore / 上传新版本任务；任务 succeeded 时切换 active。 */
  processingJobId: string | null;
  /** 版本内容 hash（新版本 dedupe / restore 内容继承；review C12）。 */
  contentHash: string;
}

interface StoredDocument {
  readonly id: string;
  readonly spaceId: string;
  readonly name: string;
  readonly mediaKind: string;
  uploadedAt: string;
  readonly usage: { readonly pages: number; readonly images: number };
  /** 服务端行版本（expected_version 依据；前端不自行递增）。 */
  version: number;
  activeVersionId: string | null;
  versions: StoredVersion[];
  /** 初始上传任务：任务 succeeded 前文档不出现在列表（生命周期 §9）。 */
  initialUploadJobId: string | null;
  /** 内容 hash（dedupe 依据）：基于文件字节而非文件名（review C12）。 */
  contentHash: string;
}

export type JobKind = 'upload' | 'reindex' | 'restore';
export type JobState =
  | 'pending'
  | 'running'
  | 'retry_wait'
  | 'succeeded'
  | 'failed'
  | 'dead_letter'
  | 'cancelled';
export type JobStage = 'queued' | 'parsing' | 'indexing';

interface StoredJob {
  readonly jobId: string;
  readonly documentId: string | null;
  readonly name: string;
  readonly spaceId: string;
  readonly uploadBatchId: string | null;
  readonly kind: JobKind;
  state: JobState;
  /** 人工重放代际：每次 replay +1；客户端按此收敛轮询。 */
  replayGeneration: number;
  stage: JobStage | null;
  nextAttemptAt: string | null;
  usage: { pages: number; images: number } | null;
  failureReason: string | null;
  ocrLowConfidence: boolean;
  notificationEventIds: string[];
  readonly createdAt: string;
  /** §10.1 超时派生标记（mock 夹具控制：running 超租约未完成）。 */
  stale: boolean;
}

interface StoredSubmission {
  readonly submissionId: string;
  version: number;
  readonly submitterUserId: string;
  readonly submitterName: string;
  /** §8.4 投稿人部门（审核列表五列之一）。 */
  readonly submitterDepartment: { id: string; name: string } | null;
  readonly targetSpaceId: string;
  readonly targetSpaceName: string;
  readonly name: string;
  readonly mediaKind: string;
  readonly sizeBytes: number;
  status: SubmissionStatus;
  readonly createdAt: string;
  reviewedAt: string | null;
  rejectReason: string | null;
  invalidatedReason: string | null;
  documentId: string | null;
  jobId: string | null;
  /** 夹具：审核时投稿人部门归属或贡献资格已变化（409 submission_scope_changed）。 */
  readonly scopeChanged: boolean;
  /** 夹具：审核时投稿人账号已冻结（409 submitter_pending_delete）。 */
  readonly submitterFrozen: boolean;
  /** 查看内容用受控文件流。 */
  content: { bytes: Uint8Array; type: string } | null;
}

interface BatchRecord {
  readonly uploadBatchId: string;
  readonly jobIds: string[];
  /** 批次统计：rejected（逐文件失败项）与 deduplicated 数（review C12）。 */
  readonly rejected: number;
  readonly deduplicated: number;
}

interface UploadFileInput {
  readonly name: string;
  readonly size: number;
  readonly type: string;
  /** 内容 hash（dedupe 依据；测试夹具可省略，缺省按 name+size 派生）。 */
  readonly contentHash?: string;
}

const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;
const DEFAULT_PAGE_SIZE = 10;
const MEDIA_KIND_BY_TYPE: Record<string, string> = {
  'application/pdf': 'pdf',
  'application/msword': 'word',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'word',
  'text/markdown': 'md',
  'text/plain': 'txt',
  'application/vnd.ms-excel': 'excel',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'excel',
};

function mediaKindOf(type: string, name: string): string {
  const byType = MEDIA_KIND_BY_TYPE[type];
  if (byType !== undefined) {
    return byType;
  }
  const ext = name.split('.').pop()?.toLowerCase() ?? '';
  switch (ext) {
    case 'pdf':
      return 'pdf';
    case 'doc':
    case 'docx':
      return 'word';
    case 'md':
      return 'md';
    case 'txt':
      return 'txt';
    case 'xls':
    case 'xlsx':
      return 'excel';
    default:
      return 'other';
  }
}

export class MockKnowledgeController {
  private readonly documents = new Map<string, StoredDocument[]>();
  /** 初始上传 claim 独立于文档活动版本；新版本激活不得改写它。 */
  private readonly initialUploadClaims = new Map<string, string>();
  private readonly jobs = new Map<string, StoredJob>();
  /** §10.1 运维任务队列独立任务池：不进上传结果层（listJobs 只迭代 jobs），cancel/replay 双池查找。 */
  private readonly opsJobs = new Map<string, StoredJob>();
  private readonly batches = new Map<string, BatchRecord>();
  private readonly submissions = new Map<string, StoredSubmission>();
  private readonly documentSeq = new Map<string, number>();
  /** 写操作幂等回放：actionKey → { payload, result }；同 key 同 payload 返回同一结果，不同 payload 409。 */
  private readonly idempotency = new Map<string, { payload: string; result: unknown }>();
  private seq = 0;
  /** 夹具：后续上传命中文件名模式的逐文件错误码（一次消费）。 */
  private nextUploadFailures: { readonly pattern: string; readonly code: string }[] = [];
  /** 夹具：后续审批动作注入的错误码（一次消费）。 */
  private nextApprovalError: string | null = null;
  /** 夹具：投稿内容受控流（submissionId → bytes/type）。 */
  private contentOverrides = new Map<string, { bytes: Uint8Array; type: string }>();

  constructor(
    private readonly auth: KnowledgeAuth,
    private readonly quota: MockQuotaStore,
    private readonly notifications: MockNotificationsController,
  ) {
    this.reset();
  }

  reset(): void {
    this.documents.clear();
    this.initialUploadClaims.clear();
    this.jobs.clear();
    this.opsJobs.clear();
    this.batches.clear();
    this.submissions.clear();
    this.documentSeq.clear();
    this.idempotency.clear();
    this.contentOverrides.clear();
    this.nextUploadFailures = [];
    this.nextApprovalError = null;
    this.seq = 0;
    this.seedFixtures();
  }

  /* ---------- 幂等回放 ---------- */

  /**
   * 幂等写：key 未见过 → 执行 action 并记录结果；key 见过且 payload 相同 → 回放结果；
   * key 见过但 payload 不同 → 409 idempotency_key_conflict（网络未知重试同 key 同体才幂等）。
   */
  /**
   * 幂等写：key 未见过 → 执行 action 并记录结果；key 见过且 payload 相同 → 回放结果；
   * key 见过但 payload 不同 → 409 idempotency_key_conflict（网络未知重试同 key 同体才幂等）。
   * 隔离维度：actor(当前用户) + endpoint(operation) + target + request payload（review C13），
   * 不同用户/不同目标/不同请求体之间绝不复用同一幂等记录。
   */
  private idempotent(key: string, payload: string, action: () => unknown, actorId: string, operation: string, target: string): unknown {
    if (key.trim() === '') {
      throw new MockHttpError(422, 'validation_error', { field: 'idempotency_key' });
    }
    const scopedKey = `${actorId}:${operation}:${target}:${key}`;
    const existing = this.idempotency.get(scopedKey);
    if (existing !== undefined) {
      if (existing.payload !== payload) {
        throw new MockHttpError(409, 'idempotency_key_conflict');
      }
      return structuredClone(existing.result) as unknown;
    }
    const result = action();
    this.idempotency.set(scopedKey, { payload, result });
    return structuredClone(result) as unknown;
  }

  /* ---------- 夹具 ---------- */

  setNextUploadFailure(pattern: string, code: string): void {
    this.nextUploadFailures.push({ pattern, code });
  }

  /** 推进任务状态机；succeeded 时按生命周期规则激活版本并登记 notification 事件（ackable）。 */
  advanceJob(auth: string | null, jobId: string, state: JobState, stage: JobStage | null = null): void {
    const user = this.auth(auth);
    const job = this.job(jobId);
    if (job.spaceId !== '' && !this.canReadSpace(user, job.spaceId)) {
      throw new MockHttpError(403, 'job_forbidden');
    }
    if (state === 'succeeded') {
      job.state = 'succeeded';
      job.stage = null;
      job.nextAttemptAt = null;
      job.failureReason = null;
      job.usage = { pages: job.usage?.pages ?? 10, images: 0 };
      const doc = job.documentId === null ? undefined : this.document(job.documentId);
      if (doc !== undefined) {
        // 生命周期：restore / 上传新版本任务成功后，processing 版本切 active，旧 active 转 superseded
        const processing = doc.versions.find((version) => version.processingJobId === job.jobId);
        if (processing !== undefined) {
          this.activateProcessingVersion(doc, processing);
        }
        // 初始上传任务成功后文档才可被列表检索（§9：job 成功前不出现在文档列表）
        if (doc.initialUploadJobId === job.jobId) {
          (doc as { initialUploadJobId: string | null }).initialUploadJobId = null;
        }
        const active = doc.versions.find((version) => version.documentVersionId === doc.activeVersionId);
        job.usage = { pages: active?.versionNumber ?? 10, images: 0 };
      }
      this.registerCompletionEvents(user.id, job);
      return;
    }
    job.state = state;
    job.stage = stage;
    if (state === 'retry_wait') {
      job.nextAttemptAt = new Date(Date.now() + 60_000).toISOString();
    } else if (state === 'running') {
      job.nextAttemptAt = null;
    }
  }

  failJob(auth: string | null, jobId: string, reason: string, deadLetter = false): void {
    const user = this.auth(auth);
    const job = this.job(jobId);
    if (!this.canReadSpace(user, job.spaceId)) {
      throw new MockHttpError(403, 'job_forbidden');
    }
    job.state = deadLetter ? 'dead_letter' : 'failed';
    job.stage = null;
    job.failureReason = reason;
    job.nextAttemptAt = null;
  }

  setRetryWait(auth: string | null, jobId: string, nextAttemptAt: string): void {
    const user = this.auth(auth);
    const job = this.job(jobId);
    if (!this.canReadSpace(user, job.spaceId)) {
      throw new MockHttpError(403, 'job_forbidden');
    }
    job.state = 'retry_wait';
    job.stage = null;
    job.nextAttemptAt = nextAttemptAt;
  }

  setNextApprovalError(code: string): void {
    this.nextApprovalError = code;
  }

  /** 审批夹具：通过/驳回投稿（投稿人侧状态联动 + 铃铛送达）。 */
  reviewSubmission(auth: string | null, submissionId: string, approved: boolean, reason: string | null): void {
    this.auth(auth);
    const submission = this.submission(submissionId);
    if (submission.status !== 'pending') {
      throw new MockHttpError(409, 'submission_already_reviewed');
    }
    submission.version += 1;
    submission.reviewedAt = new Date().toISOString();
    if (approved) {
      submission.status = 'approved';
      const doc = this.findOrCreateDocumentForSubmission(submission);
      submission.documentId = doc.id;
      const job = this.jobForApprovedSubmission(submission, doc);
      submission.jobId = job.jobId;
      this.notifications.addNotification(submission.submitterUserId, {
        type: 'submission_approved',
        title: `《${submission.name}》已通过审核`,
        payload: {
          submission_id: submission.submissionId,
          document_id: doc.id,
          job_id: job.jobId,
        },
      });
    } else {
      submission.status = 'rejected';
      submission.rejectReason = reason;
      this.notifications.addNotification(submission.submitterUserId, {
        type: 'submission_rejected',
        title: `《${submission.name}》已被驳回`,
        payload: {
          submission_id: submission.submissionId,
          ...(reason === null ? {} : { reason }),
        },
      });
    }
  }

  invalidateSubmission(auth: string | null, submissionId: string, machineCode: string): void {
    this.auth(auth);
    const submission = this.submission(submissionId);
    if (submission.status === 'approved' || submission.status === 'invalidated') {
      throw new MockHttpError(409, 'submission_state_conflict');
    }
    submission.version += 1;
    submission.status = 'invalidated';
    submission.invalidatedReason = machineCode;
    this.notifications.addNotification(submission.submitterUserId, {
      type: 'submission_invalidated',
      title: `《${submission.name}》已失效`,
      payload: { submission_id: submission.submissionId },
    });
  }

  expireSubmissionContent(auth: string | null, submissionId: string): void {
    this.auth(auth);
    const submission = this.submission(submissionId);
    submission.content = null;
  }

  purgeVersion(auth: string | null, documentId: string, versionId: string): void {
    this.auth(auth);
    const doc = this.document(documentId);
    const version = doc.versions.find((candidate) => candidate.documentVersionId === versionId);
    if (version === undefined) {
      throw new MockHttpError(404, 'document_version_not_found');
    }
    version.status = 'purged';
    version.contentAvailable = false;
    version.purgedAt = new Date().toISOString();
  }

  setSubmissionContent(auth: string | null, submissionId: string, bytes: Uint8Array, type: string): void {
    this.auth(auth);
    this.contentOverrides.set(submissionId, { bytes, type });
  }

  /* ---------- §6.2 文档列表（个人库 / 部门库 / 公共库共用） ---------- */

  listDocuments(
    auth: string | null,
    spaceId: string,
    q?: string,
    page = 1,
    pageSize = DEFAULT_PAGE_SIZE,
  ): { items: DocumentListItem[]; total: number; page: number; page_size: number } {
    const user = this.auth(auth);
    this.requireReadSpace(user, spaceId);
    const keyword = q?.trim().toLowerCase() ?? '';
    const all = (this.documents.get(spaceId) ?? [])
      // 生命周期：初始上传任务未 succeeded 的文档不出现在文档列表
      .filter((doc) => doc.initialUploadJobId === null)
      .filter((doc) => keyword === '' || doc.name.toLowerCase().includes(keyword))
      .sort((a, b) => b.uploadedAt.localeCompare(a.uploadedAt));
    if (!Number.isInteger(page) || page < 1) {
      throw new MockHttpError(422, 'validation_error', { field: 'page' });
    }
    if (!Number.isInteger(pageSize) || pageSize < 1 || pageSize > 200) {
      throw new MockHttpError(422, 'validation_error', { field: 'page_size' });
    }
    const start = (page - 1) * pageSize;
    const items = all.slice(start, start + pageSize).map((doc) => this.toListItem(doc));
    return { items, total: all.length, page, page_size: pageSize };
  }

  /* ---------- §6.3 上传 ---------- */

  uploadDocuments(
    auth: string | null,
    spaceId: string,
    files: readonly UploadFileInput[],
    idempotencyKey: string,
  ): UploadResponse {
    const user = this.auth(auth);
    const payload = JSON.stringify({ spaceId, files });
    return this.idempotent(
      idempotencyKey,
      payload,
      () => this.performUpload(user, spaceId, files),
      user.id,
      'upload-documents',
      spaceId,
    ) as UploadResponse;
  }

  private performUpload(
    user: User,
    spaceId: string,
    files: readonly UploadFileInput[],
  ): UploadResponse {
    const space = this.space(spaceId);
    const permission = this.permissionOf(user, space);
    if (permission !== 'manage' && permission !== 'contribute') {
      throw new MockHttpError(403, 'space_upload_forbidden');
    }
    if (files.length === 0) {
      throw new MockHttpError(422, 'validation_error', { field: 'files' });
    }

    const items: UploadItem[] = [];
    const accepted: { file: UploadFileInput; doc: StoredDocument; job: StoredJob; initialClaimKey: string }[] = [];

    // 管理上传在服务端事务中执行；先完成整批校验，避免错误响应留下半批文档或任务。
    for (const file of files) {
      const error = this.uploadErrorFor(file);
      if (error !== null) {
        throw new MockHttpError(422, error.code, error.details);
      }
    }

    for (const file of files) {
      // 初始上传仅在规范化文件名和内容 hash 都相同的情况下去重。
      if (permission === 'manage') {
        const hash = file.contentHash ?? this.fallbackHash(file);
        const initialClaimKey = this.initialUploadClaimKey(spaceId, file.name, hash);
        const existing = this.findInitialUploadDuplicate(initialClaimKey);
        if (existing !== undefined) {
          items.push({
            filename: file.name,
            document_id: existing.id,
            document_version_id: existing.activeVersionId,
            job_id: null,
            publication_id: null,
            deduplicated: true,
            status: 'deduplicated',
          });
          continue;
        }
        const doc = this.createDocument(space, file);
        const job = this.createJob(doc, 'upload');
        this.initialUploadClaims.set(initialClaimKey, doc.id);
        // 生命周期：初始上传任务 succeeded 前文档不出现在文档列表
        (doc as { initialUploadJobId: string | null }).initialUploadJobId = job.jobId;
        accepted.push({ file, doc, job, initialClaimKey });
        items.push({
          filename: file.name,
          document_id: doc.id,
          document_version_id: doc.activeVersionId,
          job_id: job.jobId,
          publication_id: this.nextId('publication'),
          deduplicated: false,
          status: 'pending',
        });
      } else {
        const submission = this.createSubmission(user, space, file);
        items.push({
          submission_id: submission.submissionId,
          version: submission.version,
          status: 'pending',
          space_id: spaceId,
          quota_exempt: true,
          document_id: null,
          document_version_id: null,
          job_id: null,
        });
      }
    }

    // 配额：manage 分支整批校验；contribute 不检查页额度（§6.3）。
    if (permission === 'manage') {
      const totalPages = accepted.reduce((sum, entry) => sum + this.pagesFor(entry.file), 0);
      const remaining = this.quota.remaining(user.id);
      if (totalPages > remaining) {
        // 整批拒绝：撤销已创建状态，不预扣不冻结。
        for (const entry of accepted) {
          this.jobs.delete(entry.job.jobId);
          this.initialUploadClaims.delete(entry.initialClaimKey);
          const list = this.documents.get(spaceId) ?? [];
          this.documents.set(
            spaceId,
            list.filter((candidate) => candidate.id !== entry.doc.id),
          );
        }
        throw new MockHttpError(409, 'quota_exceeded');
      }
      for (const entry of accepted) {
        this.quota.addUsage(user.id, this.pagesFor(entry.file));
      }
    }

    if (permission === 'manage') {
      const uploadBatchId = this.nextId('ub');
      const deduplicated = items.filter((item) => 'filename' in item && item.deduplicated).length;
      this.batches.set(uploadBatchId, {
        uploadBatchId,
        jobIds: accepted.map((entry) => entry.job.jobId),
        rejected: 0,
        deduplicated,
      });
      for (const entry of accepted) {
        const job = this.jobs.get(entry.job.jobId);
        if (job !== undefined) {
          (job as { uploadBatchId: string | null }).uploadBatchId = uploadBatchId;
        }
      }
      return { upload_batch_id: uploadBatchId, items };
    }
    return { items };
  }

  /* ---------- §6.4 上传新版本 ---------- */

  /**
   * 上传新版本：固定目标 document_id（不进入目标选择），单文件，携带 expected_version。
   * 创建 processing 版本 + 任务；任务 succeeded 前旧 active 继续服务（生命周期 §9）。
   * 权限：目标文档所在空间必须对当前用户为 manage。
   */
  uploadNewVersion(
    auth: string | null,
    documentId: string,
    file: UploadFileInput,
    expectedVersion: number,
    idempotencyKey: string,
  ): NewVersionResponse {
    const user = this.auth(auth);
    const payload = JSON.stringify({ documentId, name: file.name, size: file.size, type: file.type, expectedVersion });
    return this.idempotent(
      idempotencyKey,
      payload,
      () => {
        const doc = this.document(documentId);
      const space = this.space(doc.spaceId);
      if (this.permissionOf(user, space) !== 'manage') {
        throw new MockHttpError(403, 'space_upload_forbidden');
      }
      if (doc.version !== expectedVersion) {
        throw new MockHttpError(409, 'document_version_conflict');
      }
      const activeVersion = doc.versions.find((candidate) => candidate.documentVersionId === doc.activeVersionId);
      const newHash = file.contentHash ?? this.fallbackHash(file);
      if (activeVersion !== undefined && activeVersion.contentHash === newHash) {
        // 内容与当前版本一致（deduplicated）：不创建任务（review C12 新版本 dedupe 分支）。
        return {
          document_id: doc.id,
          document_version_id: activeVersion.documentVersionId,
          job_id: null,
          version: doc.version,
          deduplicated: true,
          status: 'active',
        };
      }
      const processing = this.addProcessingVersion(doc, newHash);
      const job = this.createJob(doc, 'upload');
      processing.processingJobId = job.jobId;
        return {
          document_id: doc.id,
          document_version_id: processing.documentVersionId,
          job_id: job.jobId,
          publication_id: this.nextId('publication'),
          version: doc.version,
          deduplicated: false,
          status: 'pending',
        };
      },
      user.id,
      'upload-new-version',
      documentId,
    ) as NewVersionResponse;
  }

  /* ---------- §6.6 入库任务 ---------- */

  listJobs(auth: string | null, limit?: number): { items: IngestionJob[]; limit: number; max_limit: number; has_more: boolean } {
    const user = this.auth(auth);
    const effective = limit ?? 50;
    if (!Number.isInteger(effective) || effective < 1 || effective > 200) {
      throw new MockHttpError(422, 'validation_error', { field: 'limit' });
    }
    const all = [...this.jobs.values()]
      .filter((job) => this.canReadSpace(user, job.spaceId))
      .sort((a, b) => b.createdAt.localeCompare(a.createdAt));
    const canReplay = user.role === 'ops' || user.role === 'admin';
    const items = all.slice(0, effective).map((job) => this.toJob(job, canReplay));
    return { items, limit: effective, max_limit: 200, has_more: all.length > effective };
  }

  cancelJob(auth: string | null, jobId: string, idempotencyKey = ''): void {
    const user = this.auth(auth);
    const payload = JSON.stringify({ jobId, action: 'cancel' });
    const action = (): void => {
      const job = this.job(jobId);
      if (!this.canReadSpace(user, job.spaceId)) {
        throw new MockHttpError(403, 'job_forbidden');
      }
      if (!allowedActionsFor(job).includes('cancel')) {
        throw new MockHttpError(409, 'job_state_conflict');
      }
      job.state = 'cancelled';
      job.stage = null;
      job.nextAttemptAt = null;
    };
    if (idempotencyKey === '') {
      action();
      return;
    }
    this.idempotent(idempotencyKey, payload, action, user.id, 'cancel-job', jobId);
  }

  replayJob(
    auth: string | null,
    jobId: string,
    idempotencyKey = '',
  ): { job_id: string; state: string; replay_generation: number } {
    const user = this.auth(auth);
    const payload = JSON.stringify({ jobId, action: 'replay' });
    const action = (): { job_id: string; state: string; replay_generation: number } => {
      const job = this.job(jobId);
      if (!this.canReadSpace(user, job.spaceId)) {
        throw new MockHttpError(403, 'job_forbidden');
      }
      // replay 仅允许契约规定的 ops/admin（运维人工重放）；权限校验在幂等 replay 前也执行。
      if (user.role !== 'ops' && user.role !== 'admin') {
        throw new MockHttpError(403, 'job_replay_forbidden');
      }
      if (!allowedActionsFor(job, { canReplay: true }).includes('replay')) {
        throw new MockHttpError(409, 'job_state_conflict');
      }
      job.state = 'pending';
      job.stage = 'queued';
      job.failureReason = null;
      job.nextAttemptAt = null;
      job.replayGeneration += 1;
      return {
        job_id: job.jobId,
        state: job.state,
        replay_generation: job.replayGeneration,
      };
    };
    if (idempotencyKey === '') {
      return action();
    }
    return this.idempotent(
      idempotencyKey,
      payload,
      action,
      user.id,
      'replay-job',
      jobId,
    ) as { job_id: string; state: string; replay_generation: number };
  }

  /* ---------- §10.1 运维任务队列（ops 操作 / admin 只读） ---------- */

  /**
   * 任务队列视图（§10.1–10.2）：独立任务池（seedOpsJob），四档 view 过滤。
   * allowed_actions 与 §6.6 同一规则推导，但仅 ops 可见操作；admin 行固定空数组
   * （服务端收窄，前端据此不渲染操作区，非角色分支）。stale 是派生标记，不替代 job 状态。
   */
  listOpsJobs(auth: string | null, view: OpsJobsView): { items: OpsJobItem[]; stale_count: number } {
    const user = this.auth(auth);
    if (user.role !== 'ops' && user.role !== 'admin') {
      throw new MockHttpError(403, 'ops_jobs_forbidden');
    }
    if (view !== 'all' && view !== 'active' && view !== 'replayable' && view !== 'stale') {
      throw new MockHttpError(422, 'validation_error', { field: 'view' });
    }
    const all = [...this.opsJobs.values()].sort((a, b) => a.createdAt.localeCompare(b.createdAt));
    const staleCount = all.filter((job) => job.stale).length;
    const filtered = all.filter((job) => {
      switch (view) {
        case 'active':
          return job.state === 'pending' || job.state === 'running' || job.state === 'retry_wait';
        case 'replayable':
          return job.state === 'failed' || job.state === 'cancelled' || job.state === 'dead_letter';
        case 'stale':
          return job.stale;
        default:
          return true;
      }
    });
    const items = filtered.map((job) => ({
      job_id: job.jobId,
      task_type: 'ingestion' as const,
      document_name: job.name,
      state: job.state,
      stale: job.stale,
      // 仅 ops 行携带操作；admin（超管只读）固定空数组。
      allowed_actions: user.role === 'ops' ? allowedActionsFor(job, { canReplay: true }) : [],
      enqueued_at: job.createdAt,
      wait_seconds: Math.max(0, Math.floor((Date.now() - Date.parse(job.createdAt)) / 1000)),
    }));
    return { items, stale_count: staleCount };
  }

  getUploadBatch(auth: string | null, uploadBatchId: string): UploadBatch {
    const user = this.auth(auth);
    const batch = this.batches.get(uploadBatchId);
    if (batch === undefined) {
      throw new MockHttpError(404, 'upload_batch_not_found');
    }
    const jobs = batch.jobIds.map((jobId) => this.job(jobId));
    if (jobs.some((job) => !this.canReadSpace(user, job.spaceId))) {
      throw new MockHttpError(403, 'batch_forbidden');
    }
    const count = (state: JobState) => jobs.filter((job) => job.state === state).length;
    const summary = {
      // 批次保留全部文件结果：总文件数 = 任务数 + 拒绝项 + 去重项（review C12）
      total_files: jobs.length + batch.rejected + batch.deduplicated,
      pending: count('pending'),
      running: count('running'),
      retry_wait: count('retry_wait'),
      succeeded: count('succeeded'),
      failed: count('failed'),
      cancelled: count('cancelled'),
      dead_letter: count('dead_letter'),
      rejected: batch.rejected,
      deduplicated: batch.deduplicated,
    };
    const partial = jobs.some((job) =>
      ['pending', 'running', 'retry_wait'].includes(job.state),
    );
    return {
      upload_batch_id: uploadBatchId,
      state: partial ? 'partial' : 'completed',
      summary,
    };
  }

  /* ---------- §6.9 版本记录 ---------- */

  listVersions(auth: string | null, documentId: string): {
    document_id: string;
    version: number;
    active_version_id: string | null;
    items: DocumentVersionItem[];
  } {
    const user = this.auth(auth);
    const doc = this.document(documentId);
    if (!this.canReadSpace(user, doc.spaceId)) {
      throw new MockHttpError(404, 'document_not_found');
    }
    const items = [...doc.versions]
      .sort((a, b) => b.versionNumber - a.versionNumber)
      .map((version) => ({
        document_version_id: version.documentVersionId,
        version_number: version.versionNumber,
        status: version.status,
        created_at: version.createdAt,
        activated_at: version.activatedAt,
        terminal_at: version.terminalAt,
        superseded_at: version.supersededAt,
        purge_after_at: version.purgeAfterAt,
        purged_at: version.purgedAt,
        restored_from_version_id: version.restoredFromVersionId,
        content_available: version.contentAvailable,
      }));
    return {
      document_id: doc.id,
      version: doc.version,
      active_version_id: doc.activeVersionId,
      items,
    };
  }

  restoreVersion(
    auth: string | null,
    documentId: string,
    versionId: string,
    expectedVersion: number,
    idempotencyKey: string,
  ): RestoreVersionResponse {
    const user = this.auth(auth);
    const payload = JSON.stringify({ documentId, versionId, expectedVersion });
    return this.idempotent(
      idempotencyKey,
      payload,
      () => {
        const doc = this.document(documentId);
        const space = this.space(doc.spaceId);
        if (this.permissionOf(user, space) !== 'manage') {
        throw new MockHttpError(403, 'space_upload_forbidden');
      }
      if (doc.version !== expectedVersion) {
        throw new MockHttpError(409, 'document_version_conflict');
      }
      const version = doc.versions.find((candidate) => candidate.documentVersionId === versionId);
      if (version === undefined) {
        throw new MockHttpError(404, 'document_version_not_found');
      }
      if (version.status === 'purging' || version.status === 'purged' || !version.contentAvailable) {
        throw new MockHttpError(410, 'document_version_purged');
      }
      if (version.status === 'failed' || version.status === 'cancelled') {
        throw new MockHttpError(409, 'document_version_not_restorable');
      }
      // 生命周期：恢复先创建 processing 版本（旧 active 继续服务），任务 succeeded 后才切 active。
      const processing = this.addProcessingVersion(doc, version.contentHash);
      const job = this.createJob(doc, 'restore');
      processing.processingJobId = job.jobId;
        return {
          document_id: doc.id,
          document_version_id: processing.documentVersionId,
          restored_from_version_id: version.documentVersionId,
          job_id: job.jobId,
          version: doc.version,
        };
      },
      user.id,
      'restore-version',
      documentId,
    ) as RestoreVersionResponse;
  }

  rebuildDocument(
    auth: string | null,
    documentId: string,
    expectedVersion: number,
    idempotencyKey: string,
  ): RebuildDocumentResponse {
    const user = this.auth(auth);
    const payload = JSON.stringify({ documentId, expectedVersion });
    return this.idempotent(
      idempotencyKey,
      payload,
      () => {
        const doc = this.document(documentId);
        const space = this.space(doc.spaceId);
        if (this.permissionOf(user, space) !== 'manage') {
        throw new MockHttpError(403, 'space_upload_forbidden');
      }
      if (doc.version !== expectedVersion) {
        throw new MockHttpError(409, 'document_version_conflict');
      }
        const job = this.createJob(doc, 'reindex');
        return { document_id: doc.id, document_version_id: doc.activeVersionId ?? '', job_id: job.jobId, version: doc.version };
      },
      user.id,
      'reindex-document',
      documentId,
    ) as RebuildDocumentResponse;
  }

  deleteDocument(auth: string | null, documentId: string, expectedVersion: number, idempotencyKey = ''): void {
    const user = this.auth(auth);
    const payload = JSON.stringify({ documentId, expectedVersion });
    const action = (): void => {
      const doc = this.document(documentId);
      const space = this.space(doc.spaceId);
      if (this.permissionOf(user, space) !== 'manage') {
        throw new MockHttpError(403, 'space_upload_forbidden');
      }
      if (doc.version !== expectedVersion) {
        throw new MockHttpError(409, 'document_version_conflict');
      }
      const list = this.documents.get(doc.spaceId) ?? [];
      this.documents.set(doc.spaceId, list.filter((candidate) => candidate.id !== doc.id));
      this.documentSeq.delete(doc.id);
    };
    if (idempotencyKey === '') {
      action();
      return;
    }
    this.idempotent(idempotencyKey, payload, action, user.id, 'delete-document', documentId);
  }

  /* ---------- §6.10 投稿 ---------- */

  listSubmissions(auth: string | null, status: SubmissionStatus | 'all'): { items: Submission[] } {
    const user = this.auth(auth);
    const items = [...this.submissions.values()]
      .filter((submission) => submission.submitterUserId === user.id)
      .filter((submission) => status === 'all' || submission.status === status)
      .sort((a, b) => b.createdAt.localeCompare(a.createdAt))
      .map((submission) => this.toSubmission(submission));
    return { items };
  }

  getSubmissionContent(auth: string | null, submissionId: string): { bytes: Uint8Array; type: string } {
    const user = this.auth(auth);
    const submission = this.submission(submissionId);
    // 权限：投稿人本人可读；审核者可读其 §8.4 审核范围内投稿内容（行内「查看内容」新窗口打开
    // 待审原文件）——范围与 listApprovals 同一口径（reviewScopeSpaceIds：部长=本部门 manage 空间；
    // 运维=公共库；超管=公共库+全部 active 部门）。
    const canRead =
      submission.submitterUserId === user.id ||
      this.reviewScopeSpaceIds(user).includes(submission.targetSpaceId);
    if (!canRead) {
      throw new MockHttpError(403, 'submission_forbidden');
    }
    const content = this.contentOverrides.get(submissionId) ?? submission.content;
    if (content === null) {
      throw new MockHttpError(404, 'submission_content_unavailable');
    }
    return content;
  }

  withdrawSubmission(
    auth: string | null,
    submissionId: string,
    expectedVersion: number,
    idempotencyKey: string,
  ): WithdrawSubmissionResponse {
    const user = this.auth(auth);
    const payload = JSON.stringify({ submissionId, expectedVersion });
    return this.idempotent(
      idempotencyKey,
      payload,
      () => {
        const submission = this.submission(submissionId);
      if (submission.submitterUserId !== user.id) {
        throw new MockHttpError(403, 'submission_forbidden');
      }
      if (submission.version !== expectedVersion) {
        throw new MockHttpError(409, 'version_conflict');
      }
      if (submission.status !== 'pending') {
        throw new MockHttpError(409, 'submission_state_conflict');
      }
      submission.version += 1;
      submission.status = 'withdrawn';
      // 契约 §6.10：撤回响应只含 { submission_id, version, status }；前端与原行合并。
        return {
          submission_id: submission.submissionId,
          version: submission.version,
          status: 'withdrawn',
        };
      },
      user.id,
      'withdraw-submission',
      submissionId,
    ) as WithdrawSubmissionResponse;
  }

  deleteSubmission(auth: string | null, submissionId: string, expectedVersion: number, idempotencyKey = ''): void {
    const user = this.auth(auth);
    const payload = JSON.stringify({ submissionId, expectedVersion });
    const action = (): void => {
      const submission = this.submission(submissionId);
      if (submission.submitterUserId !== user.id) {
        throw new MockHttpError(403, 'submission_forbidden');
      }
      if (submission.version !== expectedVersion) {
        throw new MockHttpError(409, 'version_conflict');
      }
      if (submission.status === 'pending' || submission.status === 'approved') {
        throw new MockHttpError(409, 'submission_state_conflict');
      }
      this.submissions.delete(submissionId);
    };
    if (idempotencyKey === '') {
      action();
      return;
    }
    this.idempotent(idempotencyKey, payload, action, user.id, 'delete-submission', submissionId);
  }

  /* ---------- §8.1 / §8.4–8.5 部长部门库审核 ---------- */

  /** §6.1 上传目标空间（usage=upload）：permission=manage/contribute 的空间。 */
  listSpacesForUpload(auth: string | null): { items: { id: string; kind: string; name: string; permission: string; document_count: number }[] } {
    const user = this.auth(auth);
    const items = SPACE_DEFS.filter((space) => {
      const permission = this.permissionOf(user, space);
      return permission === 'manage' || permission === 'contribute';
    }).map((space) => ({
      id: space.id,
      kind: space.kind,
      name: space.name,
      permission: this.permissionOf(user, space) as string,
      document_count: 0,
    }));
    return { items };
  }

  managedDepartmentSpaceIds(user: User): string[] {
    return SPACE_DEFS.filter(
      (space) =>
        space.kind === 'department' &&
        this.permissionOf(user, space) === 'manage',
    ).map((space) => space.id);
  }

  /**
   * §8.4 投稿审核范围（角色 × 目标空间，唯一权威）：
   * 部长=本部门 manage 空间；运维=公共库；超管=公共库+全部 active 部门；普通用户无审核范围。
   */
  reviewScopeSpaceIds(user: User): string[] {
    if (user.role === 'ops') {
      return ['public'];
    }
    if (user.role === 'admin') {
      // §8.4：超管 = 公共库 + 全部 active 部门；已停用部门空间不在审核范围
      return [
        'public',
        ...SPACE_DEFS.filter(
          (space) => space.kind === 'department' && space.inactive !== true,
        ).map((space) => space.id),
      ];
    }
    if (user.role === 'minister') {
      return this.managedDepartmentSpaceIds(user);
    }
    return [];
  }

  /** §8.1 审批计数徽标：按当前角色范围计数（quota_pending 恒 0；运维配额计数由 admin 域覆盖）。 */
  getApprovalSummary(auth: string | null): ApprovalSummary {
    const user = this.auth(auth);
    const scope = this.reviewScopeSpaceIds(user);
    const submissionPending = [...this.submissions.values()].filter(
      (submission) => submission.status === 'pending' && scope.includes(submission.targetSpaceId),
    ).length;
    return { quota_pending: 0, submission_pending: submissionPending };
  }

  /** admin 域计数联动：指定空间集合的 pending 投稿数（内部口径，不经鉴权）。 */
  countPendingSubmissions(spaceIds: readonly string[]): number {
    return [...this.submissions.values()].filter(
      (submission) => submission.status === 'pending' && spaceIds.includes(submission.targetSpaceId),
    ).length;
  }

  /** §8.4 待审列表：只返回当前审核者有权处理的 pending 投稿（正序，先投先审）。 */
  listApprovals(auth: string | null): { items: ApprovalListItem[] } {
    const user = this.auth(auth);
    const scope = this.reviewScopeSpaceIds(user);
    if (scope.length === 0) {
      throw new MockHttpError(403, 'approval_forbidden');
    }
    const items = [...this.submissions.values()]
      .filter((submission) => submission.status === 'pending' && scope.includes(submission.targetSpaceId))
      .sort((a, b) => a.createdAt.localeCompare(b.createdAt))
      .map((submission) => this.toSubmission(submission));
    return { items };
  }

  /** 审核前置校验共用：范围 / 状态 / 版本 / 资格变化（§8.5 错误系列）。 */
  private requireReviewableSubmission(
    user: User,
    submissionId: string,
    expectedVersion: number,
  ): StoredSubmission {
    this.consumeApprovalError();
    const submission = this.submission(submissionId);
    if (!this.reviewScopeSpaceIds(user).includes(submission.targetSpaceId)) {
      throw new MockHttpError(403, 'approval_forbidden');
    }
    if (submission.status !== 'pending') {
      throw new MockHttpError(409, 'submission_already_reviewed');
    }
    if (submission.version !== expectedVersion) {
      throw new MockHttpError(409, 'version_conflict');
    }
    // 投稿人部门归属或贡献资格已变化：后端已置 invalidated，details 带最新 version。
    if (submission.scopeChanged) {
      this.markSubmissionInvalidated(submission, 'scope_changed');
      throw new MockHttpError(409, 'submission_scope_changed', { version: submission.version });
    }
    // 投稿人账号已冻结：投稿进入 invalidated，审核侧刷新列表。
    if (submission.submitterFrozen) {
      this.markSubmissionInvalidated(submission, 'submitter_pending_delete');
      throw new MockHttpError(409, 'submitter_pending_delete');
    }
    return submission;
  }

  /** 审核竞态置失效：版本递增 + invalidated + 铃铛送达投稿人（§13 submission_invalidated）。 */
  private markSubmissionInvalidated(submission: StoredSubmission, machineCode: string): void {
    submission.version += 1;
    submission.status = 'invalidated';
    submission.invalidatedReason = machineCode;
    this.notifications.addNotification(submission.submitterUserId, {
      type: 'submission_invalidated',
      title: `《${submission.name}》已失效`,
      payload: { submission_id: submission.submissionId, reason: machineCode },
    });
  }

  approveSubmission(
    auth: string | null,
    submissionId: string,
    expectedVersion: number,
    idempotencyKey = '',
  ): ApprovalDecisionResponse {
    const user = this.auth(auth);
    const payload = JSON.stringify({ submissionId, expectedVersion, action: 'approve' });
    const action = (): ApprovalDecisionResponse => {
      const submission = this.requireReviewableSubmission(user, submissionId, expectedVersion);
      // 目标空间已存在重复文档：投稿保持 pending，行不移除（§8.5 duplicate_document）。
      if (this.findByName(submission.targetSpaceId, submission.name) !== undefined) {
        throw new MockHttpError(409, 'duplicate_document');
      }
      submission.version += 1;
      submission.reviewedAt = new Date().toISOString();
      submission.status = 'approved';
      const doc = this.findOrCreateDocumentForSubmission(submission);
      submission.documentId = doc.id;
      const job = this.jobForApprovedSubmission(submission, doc);
      submission.jobId = job.jobId;
      this.notifications.addNotification(submission.submitterUserId, {
        type: 'submission_approved',
        title: `《${submission.name}》已通过审核`,
        payload: { submission_id: submission.submissionId, document_id: doc.id, job_id: job.jobId },
      });
      return {
        submission_id: submission.submissionId,
        version: submission.version,
        status: 'approved',
        document_id: doc.id,
        document_version_id: doc.activeVersionId ?? undefined,
        job_id: job.jobId,
      };
    };
    if (idempotencyKey === '') {
      return action();
    }
    return this.idempotent(
      idempotencyKey,
      payload,
      action,
      user.id,
      'approval-decision',
      submissionId,
    ) as ApprovalDecisionResponse;
  }

  rejectSubmission(
    auth: string | null,
    submissionId: string,
    expectedVersion: number,
    reason: string | null,
    idempotencyKey = '',
  ): ApprovalDecisionResponse {
    const user = this.auth(auth);
    const payload = JSON.stringify({ submissionId, expectedVersion, action: 'reject', reason });
    const action = (): ApprovalDecisionResponse => {
      const submission = this.requireReviewableSubmission(user, submissionId, expectedVersion);
      submission.version += 1;
      submission.reviewedAt = new Date().toISOString();
      submission.status = 'rejected';
      submission.rejectReason = reason;
      this.notifications.addNotification(submission.submitterUserId, {
        type: 'submission_rejected',
        title: `《${submission.name}》已被驳回`,
        payload: { submission_id: submission.submissionId, ...(reason === null ? {} : { reason }) },
      });
      return {
        submission_id: submission.submissionId,
        version: submission.version,
        status: 'rejected',
      };
    };
    if (idempotencyKey === '') {
      return action();
    }
    return this.idempotent(
      idempotencyKey,
      payload,
      action,
      user.id,
      'approval-decision',
      submissionId,
    ) as ApprovalDecisionResponse;
  }

  /* ---------- 内部 ---------- */

  private seedFixtures(): void {
    // 个人库种子（zhangsan；与 chat-contract 同名的文档名，保证检索范围 chip 行为一致）
    const personalZhangsan: [string, string, string, number, number][] = [
      ['员工手册.pdf', 'pdf', '2026-07-20T02:00:00Z', 50, 40],
      ['报销制度.docx', 'word', '2026-07-18T09:30:00Z', 12, 0],
      ['年假政策.md', 'md', '2026-07-10T04:00:00Z', 3, 0],
    ];
    for (const [name, kind, createdAt, pages, images] of personalZhangsan) {
      this.seedDocument('personal:u_user', name, kind, createdAt, pages, images);
    }
    // 部长个人库 + 财务部部门库
    this.seedDocument('personal:u_minister', '部门预算汇总.xlsx', 'excel', '2026-07-15T01:00:00Z', 8, 0);
    this.seedDocument('department:d_finance', '财务审批流程.pdf', 'pdf', '2026-07-01T00:00:00Z', 30, 5);
    this.seedDocument('department:d_finance', '差旅报销标准.docx', 'word', '2026-06-20T00:00:00Z', 15, 2);
    // 管理端只读下钻种子（§7.3）：与 admin 用户/部门种子的 document_count 对齐
    this.seedDocument('personal:u_chen', '入职培训笔记.md', 'md', '2026-07-12T02:00:00Z', 5, 0);
    this.seedDocument('personal:u_chen', '人事政策摘编.pdf', 'pdf', '2026-07-18T06:00:00Z', 18, 2);
    this.seedDocument('department:d_legacy', '2019 年档案汇编.pdf', 'pdf', '2026-03-01T00:00:00Z', 120, 8);
    this.seedDocument('department:d_legacy', '2020 年档案汇编.pdf', 'pdf', '2026-03-02T00:00:00Z', 96, 6);
    this.seedDocument('department:d_legacy', '2021 年档案汇编.pdf', 'pdf', '2026-03-03T00:00:00Z', 88, 4);
    this.seedDocument('department:d_legacy', '档案借阅登记.xlsx', 'excel', '2026-03-04T00:00:00Z', 12, 0);
    // 公共库
    this.seedDocument('public', '公共制度汇编.pdf', 'pdf', '2026-06-01T00:00:00Z', 200, 10);

    // 部长部门库待审核投稿种子（审批徽标 + 列表数据源）
    const finance = { id: 'd_finance', name: '财务部' };
    this.seedSubmission('u_user', 'zhangsan', 'department:d_finance', '财务部', '第三季度预算说明.pdf', 'pdf', 2048, '2026-07-25T02:00:00Z', { submitterDepartment: finance });
    this.seedSubmission('u_user', 'zhangsan', 'department:d_finance', '财务部', '费用报销细则修订.docx', 'word', 1024, '2026-07-26T03:00:00Z', { submitterDepartment: finance });

    // 「我的投稿」五态展示种子（u_user）：approved / rejected（含原因）/ withdrawn / invalidated
    this.seedSubmission('u_user', 'zhangsan', 'public', '公共库', '入职指引图文版.pdf', 'pdf', 2560, '2026-07-20T01:00:00Z', { submitterDepartment: finance, status: 'approved' });
    this.seedSubmission('u_user', 'zhangsan', 'public', '公共库', '部门团建方案.docx', 'word', 900, '2026-07-18T02:00:00Z', { submitterDepartment: finance, status: 'rejected', rejectReason: '内容与公共库现有文档重复' });
    this.seedSubmission('u_user', 'zhangsan', 'department:d_finance', '财务部', '废弃的预算草稿.xlsx', 'excel', 700, '2026-07-15T03:00:00Z', { submitterDepartment: finance, status: 'withdrawn' });
    this.seedSubmission('u_user', 'zhangsan', 'department:d_finance', '财务部', '过期报销模板.docx', 'word', 600, '2026-07-10T04:00:00Z', { submitterDepartment: finance, status: 'invalidated', invalidatedReason: 'submission_scope_changed' });

    // 上传结果层任务卡种子（u_user 个人库）：解析中 / 已入库（含低置信标记）/ 失败，覆盖卡状态呈现
    this.seedUploadJob({ name: '扫描合同副本.pdf', spaceId: 'personal:u_user', state: 'running', stage: 'parsing', minutesAgo: 12 });
    this.seedUploadJob({
      name: '历史培训材料.pdf',
      spaceId: 'personal:u_user',
      state: 'succeeded',
      usage: { pages: 18, images: 4 },
      ocrLowConfidence: true,
      minutesAgo: 90,
    });
    this.seedUploadJob({ name: '损坏的演示文稿.pptx', spaceId: 'personal:u_user', state: 'failed', failureReason: '文件格式不受支持', minutesAgo: 240 });

    // 运维 / 超管审核范围种子（§8.4：ops=公共库；admin=公共库+全部 active 部门）。
    // 时间正序先投先审；各错误路径各一条种子（duplicate / scope_changed / 冻结投稿人）。
    this.seedSubmission('u_user', 'zhangsan', 'public', '公共库', '行业研报汇总.pdf', 'pdf', 4096, '2026-07-27T01:00:00Z', { submitterDepartment: finance });
    this.seedSubmission('u_minister', 'minister-li', 'public', '公共库', '公共制度汇编.pdf', 'pdf', 8192, '2026-07-27T02:00:00Z', { submitterDepartment: finance });
    this.seedSubmission('u_user', 'zhangsan', 'public', '公共库', '跨部门协作指引.pdf', 'pdf', 3072, '2026-07-27T03:00:00Z', { submitterDepartment: finance, scopeChanged: true });
    this.seedSubmission('u_ghost', 'ghost', 'public', '公共库', '历史遗留材料.pdf', 'pdf', 1024, '2026-07-27T04:00:00Z', { submitterFrozen: true });
    this.seedSubmission('u_extra_wang', 'wangwu', 'department:d_hr', '人事部', '招聘流程优化.docx', 'word', 1536, '2026-07-27T05:00:00Z', { submitterDepartment: { id: 'd_hr', name: '人事部' } });

    // §10.1 运维任务队列种子（独立任务池，不进上传结果层）：
    // 超时 running ×2（stale 徽标数据源）、正常 running、pending、retry_wait、
    // failed、dead_letter、cancelled、succeeded 各一，覆盖四档 view。
    this.seedOpsJob({ name: '事故报告.pdf', state: 'running', stale: true, enqueuedSecondsAgo: 3400 });
    this.seedOpsJob({ name: '库存盘点.pdf', state: 'running', stale: true, enqueuedSecondsAgo: 3900 });
    this.seedOpsJob({ name: '月度归档.pdf', state: 'running', enqueuedSecondsAgo: 120 });
    this.seedOpsJob({ name: '票据扫描.pdf', state: 'pending', enqueuedSecondsAgo: 300 });
    this.seedOpsJob({ name: '人事表格.xlsx', state: 'retry_wait', enqueuedSecondsAgo: 720 });
    this.seedOpsJob({ name: '损坏的文档.pdf', state: 'failed', enqueuedSecondsAgo: 1800 });
    this.seedOpsJob({ name: '超大附件.pdf', state: 'dead_letter', enqueuedSecondsAgo: 7200 });
    this.seedOpsJob({ name: '已取消任务.pdf', state: 'cancelled', enqueuedSecondsAgo: 5400 });
    this.seedOpsJob({ name: '已完成入库.pdf', state: 'succeeded', enqueuedSecondsAgo: 86400 });
  }

  /** §10.1 任务队列种子夹具：独立任务池（opsJobs），listJobs / 上传结果层不可见。 */
  seedOpsJob(input: {
    readonly name: string;
    readonly state: JobState;
    readonly stale?: boolean;
    readonly enqueuedSecondsAgo?: number;
  }): StoredJob {
    const createdAt = new Date(Date.now() - (input.enqueuedSecondsAgo ?? 0) * 1000).toISOString();
    const job: StoredJob = {
      jobId: this.nextId('opsjob'),
      documentId: null,
      name: input.name,
      spaceId: 'public',
      uploadBatchId: null,
      kind: 'upload',
      state: input.state,
      replayGeneration: 0,
      stage:
        input.state === 'pending'
          ? 'queued'
          : input.state === 'running'
            ? 'parsing'
            : null,
      nextAttemptAt:
        input.state === 'retry_wait'
          ? new Date(Date.now() + 60_000).toISOString()
          : null,
      usage: input.state === 'succeeded' ? { pages: 20, images: 0 } : null,
      failureReason:
        input.state === 'failed' || input.state === 'dead_letter' ? 'parse_error' : null,
      ocrLowConfidence: false,
      notificationEventIds: [],
      createdAt,
      stale: input.stale ?? false,
    };
    this.opsJobs.set(job.jobId, job);
    return job;
  }

  private seedDocument(spaceId: string, name: string, mediaKind: string, createdAt: string, pages: number, images: number): StoredDocument {
    const seq = (this.documentSeq.get(spaceId) ?? 0) + 1;
    this.documentSeq.set(spaceId, seq);
    const doc: StoredDocument = {
      id: `doc_${spaceId.replace(/[^a-zA-Z0-9]/g, '_')}_${seq}`,
      spaceId,
      name,
      mediaKind,
      uploadedAt: createdAt,
      usage: { pages, images },
      version: 1,
      activeVersionId: null,
      versions: [],
      initialUploadJobId: null,
      contentHash: `seed:${name}:${createdAt}`,
    };
    const version: StoredVersion = {
      documentVersionId: this.nextId('dv'),
      versionNumber: 1,
      status: 'active',
      createdAt,
      activatedAt: createdAt,
      terminalAt: null,
      supersededAt: null,
      purgeAfterAt: null,
      purgedAt: null,
      restoredFromVersionId: null,
      contentAvailable: true,
      processingJobId: null,
      contentHash: `seed:${name}:${createdAt}`,
    };
    doc.versions.push(version);
    doc.activeVersionId = version.documentVersionId;
    const list = this.documents.get(spaceId) ?? [];
    list.push(doc);
    this.documents.set(spaceId, list);
    return doc;
  }

  /** §6.6 上传结果层任务卡种子（jobs 池，区别于 §10.1 运维任务队列的 opsJobs 池）。 */
  private seedUploadJob(input: {
    readonly name: string;
    readonly spaceId: string;
    readonly state: JobState;
    readonly stage?: JobStage;
    readonly usage?: { pages: number; images: number } | null;
    readonly failureReason?: string | null;
    readonly ocrLowConfidence?: boolean;
    readonly minutesAgo?: number;
  }): StoredJob {
    const job: StoredJob = {
      jobId: this.nextId('job'),
      documentId: null,
      name: input.name,
      spaceId: input.spaceId,
      uploadBatchId: null,
      kind: 'upload',
      state: input.state,
      replayGeneration: 0,
      stage: input.stage ?? null,
      nextAttemptAt: null,
      usage: input.usage ?? null,
      failureReason: input.failureReason ?? null,
      ocrLowConfidence: input.ocrLowConfidence ?? false,
      notificationEventIds: [],
      createdAt: new Date(Date.now() - (input.minutesAgo ?? 0) * 60_000).toISOString(),
      stale: false,
    };
    this.jobs.set(job.jobId, job);
    return job;
  }

  private seedSubmission(
    submitterUserId: string,
    submitterName: string,
    targetSpaceId: string,
    targetSpaceName: string,
    name: string,
    mediaKind: string,
    sizeBytes: number,
    createdAt: string,
    options: {
      readonly submitterDepartment?: { id: string; name: string } | null;
      readonly scopeChanged?: boolean;
      readonly submitterFrozen?: boolean;
      /** 初始状态（默认 pending）；非 pending 用于「我的投稿」五态展示种子。 */
      readonly status?: SubmissionStatus;
      readonly rejectReason?: string;
      readonly invalidatedReason?: string;
    } = {},
  ): StoredSubmission {
    const submission: StoredSubmission = {
      submissionId: this.nextId('sub'),
      version: 1,
      submitterUserId,
      submitterName,
      submitterDepartment: options.submitterDepartment ?? null,
      targetSpaceId,
      targetSpaceName,
      name,
      mediaKind,
      sizeBytes,
      status: options.status ?? 'pending',
      createdAt,
      reviewedAt: options.status !== undefined && options.status !== 'pending' ? createdAt : null,
      rejectReason: options.rejectReason ?? null,
      invalidatedReason: options.invalidatedReason ?? null,
      documentId: null,
      jobId: null,
      scopeChanged: options.scopeChanged ?? false,
      submitterFrozen: options.submitterFrozen ?? false,
      content: { bytes: new TextEncoder().encode(`mock content for ${name}`), type: 'application/pdf' },
    };
    this.submissions.set(submission.submissionId, submission);
    return submission;
  }

  private space(spaceId: string): SpaceDef {
    const space = SPACE_DEFS.find((candidate) => candidate.id === spaceId);
    if (space === undefined) {
      throw new MockHttpError(404, 'space_not_found');
    }
    return space;
  }

  private permissionOf(
    user: User,
    space: SpaceDef,
  ): 'manage' | 'read' | 'contribute' | null {
    if (space.isPublic === true) {
      return user.role === 'ops' || user.role === 'admin' ? 'manage' : 'contribute';
    }
    if (space.kind === 'personal') {
      return space.ownerUserId === user.id ? 'manage' : 'read';
    }
    if (space.kind === 'department') {
      if (user.role === 'ops' || user.role === 'admin') {
        return 'manage';
      }
      if (user.department?.id !== null && space.departmentId === user.department?.id) {
        return user.role === 'minister' ? 'manage' : 'read';
      }
      return null;
    }
    return null;
  }

  private canReadSpace(user: User, spaceId: string): boolean {
    return this.permissionOf(user, this.space(spaceId)) !== null;
  }

  private requireReadSpace(user: User, spaceId: string): void {
    const space = this.space(spaceId);
    if (this.permissionOf(user, space) === null) {
      throw new MockHttpError(404, 'space_not_found');
    }
  }

  private findByName(spaceId: string, name: string): StoredDocument | undefined {
    return (this.documents.get(spaceId) ?? []).find((doc) => doc.name === name);
  }

  private findInitialUploadDuplicate(initialClaimKey: string): StoredDocument | undefined {
    const documentId = this.initialUploadClaims.get(initialClaimKey);
    if (documentId === undefined) {
      return undefined;
    }
    return [...this.documents.values()].flat().find((document) => document.id === documentId);
  }

  private initialUploadClaimKey(spaceId: string, filename: string, contentHash: string): string {
    return JSON.stringify([spaceId, this.normalizeFilename(filename), contentHash]);
  }

  private normalizeFilename(filename: string): string {
    // JavaScript has no native Unicode casefold. These mappings cover the backend-reachable
    // case-fold differences relevant to user-facing filenames without adding a new dependency.
    return filename.trim().replace(/\s+/g, ' ').toLowerCase().replaceAll('ß', 'ss').replaceAll('ς', 'σ');
  }

  /** 缺省 hash：夹具未提供 contentHash 时按 name+size 派生（保持既有测试语义）。 */
  private fallbackHash(file: UploadFileInput): string {
    return `fb:${file.name}:${file.size}`;
  }

  private createDocument(space: SpaceDef, file: UploadFileInput): StoredDocument {
    const now = new Date().toISOString();
    const seq = (this.documentSeq.get(space.id) ?? 0) + 1;
    this.documentSeq.set(space.id, seq);
    const doc: StoredDocument = {
      id: this.nextId('doc'),
      spaceId: space.id,
      name: file.name,
      mediaKind: mediaKindOf(file.type, file.name),
      uploadedAt: now,
      usage: { pages: this.pagesFor(file), images: 0 },
      version: 1,
      activeVersionId: null,
      versions: [],
      initialUploadJobId: null,
      contentHash: file.contentHash ?? this.fallbackHash(file),
    };
    const version: StoredVersion = {
      documentVersionId: this.nextId('dv'),
      versionNumber: 1,
      status: 'active',
      createdAt: now,
      activatedAt: now,
      terminalAt: null,
      supersededAt: null,
      purgeAfterAt: null,
      purgedAt: null,
      restoredFromVersionId: null,
      contentAvailable: true,
      processingJobId: null,
      contentHash: file.contentHash ?? this.fallbackHash(file),
    };
    doc.versions.push(version);
    doc.activeVersionId = version.documentVersionId;
    const list = this.documents.get(space.id) ?? [];
    list.push(doc);
    this.documents.set(space.id, list);
    return doc;
  }

  /**
   * 新增 processing 版本（restore / 上传新版本）：任务 succeeded 前旧 active 继续服务，
   * 新版本不激活、不 supersede 旧版本。
   */
  private addProcessingVersion(doc: StoredDocument, contentHash: string): StoredVersion {
    doc.version += 1;
    const now = new Date().toISOString();
    const version: StoredVersion = {
      documentVersionId: this.nextId('dv'),
      versionNumber: doc.version,
      status: 'processing',
      createdAt: now,
      activatedAt: null,
      terminalAt: null,
      supersededAt: null,
      purgeAfterAt: null,
      purgedAt: null,
      restoredFromVersionId: null,
      contentAvailable: true,
      processingJobId: null,
      contentHash,
    };
    doc.versions.push(version);
    return version;
  }

  /** 任务 succeeded 时：processing 版本切 active，旧 active 转 superseded（生命周期 §9）。 */
  private activateProcessingVersion(doc: StoredDocument, processing: StoredVersion): void {
    const now = new Date().toISOString();
    for (const version of doc.versions) {
      if (version.status === 'active' && version.documentVersionId !== processing.documentVersionId) {
        version.status = 'superseded';
        version.supersededAt = now;
        version.terminalAt = now;
        version.purgeAfterAt = new Date(Date.now() + 30 * 24 * 3600 * 1000).toISOString();
      }
    }
    processing.status = 'active';
    processing.activatedAt = now;
    processing.processingJobId = null;
    doc.activeVersionId = processing.documentVersionId;
    doc.uploadedAt = now;
    (doc as { contentHash: string }).contentHash = processing.contentHash;
  }

  private createJob(doc: StoredDocument, kind: JobKind): StoredJob {
    const job: StoredJob = {
      jobId: this.nextId('job'),
      documentId: doc.id,
      name: doc.name,
      spaceId: doc.spaceId,
      uploadBatchId: null,
      kind,
      state: 'pending',
      replayGeneration: 0,
      stage: 'queued',
      nextAttemptAt: null,
      usage: null,
      failureReason: null,
      ocrLowConfidence: false,
      notificationEventIds: [],
      createdAt: new Date().toISOString(),
      stale: false,
    };
    this.jobs.set(job.jobId, job);
    return job;
  }

  private createSubmission(user: User, space: SpaceDef, file: UploadFileInput): StoredSubmission {
    const submission: StoredSubmission = {
      submissionId: this.nextId('sub'),
      version: 1,
      submitterUserId: user.id,
      submitterName: user.display_name,
      submitterDepartment: user.department,
      targetSpaceId: space.id,
      targetSpaceName: space.name,
      name: file.name,
      mediaKind: mediaKindOf(file.type, file.name),
      sizeBytes: file.size,
      status: 'pending',
      createdAt: new Date().toISOString(),
      reviewedAt: null,
      rejectReason: null,
      invalidatedReason: null,
      documentId: null,
      jobId: null,
      scopeChanged: false,
      submitterFrozen: false,
      content: { bytes: new TextEncoder().encode(`mock content for ${file.name}`), type: file.type || 'application/octet-stream' },
    };
    this.submissions.set(submission.submissionId, submission);
    return submission;
  }

  private findOrCreateDocumentForSubmission(submission: StoredSubmission): StoredDocument {
    const existing = this.findByName(submission.targetSpaceId, submission.name);
    if (existing !== undefined) {
      return existing;
    }
    const space = this.space(submission.targetSpaceId);
    const doc = this.createDocument(space, {
      name: submission.name,
      size: submission.sizeBytes,
      type: 'application/octet-stream',
    });
    // 审核通过创建的文档与初始上传同一生命周期：job 成功前不可检索（review C11）。
    const job = this.createJob(doc, 'upload');
    (doc as { initialUploadJobId: string | null }).initialUploadJobId = job.jobId;
    return doc;
  }

  /** 审批通过：新文档复用其初始上传任务；已存在文档新建上传任务。 */
  private jobForApprovedSubmission(submission: StoredSubmission, doc: StoredDocument): StoredJob {
    const initialJobId = doc.initialUploadJobId;
    if (initialJobId !== null && this.jobs.has(initialJobId)) {
      return this.jobs.get(initialJobId) as StoredJob;
    }
    const job = this.createJob(doc, 'upload');
    submission.jobId = job.jobId;
    return job;
  }

  private uploadErrorFor(file: UploadFileInput): { code: string; message: string; details: Record<string, unknown> } | null {
    const failure = this.nextUploadFailures.find((candidate) => file.name.includes(candidate.pattern));
    if (failure !== undefined) {
      this.nextUploadFailures = this.nextUploadFailures.filter(
        (candidate) => candidate !== failure,
      );
      return { code: failure.code, message: failure.code, details: { file: file.name } };
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      return { code: 'upload_too_large', message: 'upload_too_large', details: { file: file.name } };
    }
    if (file.name.endsWith('.zip') || file.name.endsWith('.tar.gz')) {
      return { code: 'unsafe_archive', message: 'unsafe_archive', details: { file: file.name } };
    }
    if (file.name.includes('malware')) {
      return { code: 'malware_detected', message: 'malware_detected', details: { file: file.name } };
    }
    if (file.type === 'text/plain' && file.name.endsWith('.pdf')) {
      return {
        code: 'upload_content_type_mismatch',
        message: 'upload_content_type_mismatch',
        details: { file: file.name },
      };
    }
    const kind = mediaKindOf(file.type, file.name);
    if (kind === 'other' && file.type !== '') {
      return {
        code: 'unsupported_media_type',
        message: 'unsupported_media_type',
        details: { file: file.name },
      };
    }
    return null;
  }

  private pagesFor(file: UploadFileInput): number {
    const match = /页(\d+)/.exec(file.name);
    return match === null ? 10 : Number(match[1]);
  }

  private registerCompletionEvents(userId: string, job: StoredJob): void {
    const events: string[] = [];
    const ingestionEvent = `evt_ingestion_${job.jobId}`;
    this.notifications.registerPendingEvent(userId, ingestionEvent, 'ingestion_completed');
    this.notifications.addNotification(userId, {
      type: 'ingestion_completed',
      title: `《${job.name}》已解析完成并入库`,
      payload: { job_id: job.jobId, document_id: job.documentId ?? '' },
      eventId: ingestionEvent,
    });
    events.push(ingestionEvent);
    if (job.ocrLowConfidence) {
      const ocrEvent = `evt_ocr_${job.jobId}`;
      this.notifications.registerPendingEvent(userId, ocrEvent, 'ocr_low_confidence');
      this.notifications.addNotification(userId, {
        type: 'ocr_low_confidence',
        title: `《${job.name}》识别置信度较低`,
        payload: { job_id: job.jobId, document_id: job.documentId ?? '' },
        eventId: ocrEvent,
      });
      events.push(ocrEvent);
    }
    job.notificationEventIds = events;
  }

  private consumeApprovalError(): void {
    if (this.nextApprovalError !== null) {
      const code = this.nextApprovalError;
      this.nextApprovalError = null;
      throw new MockHttpError(409, code);
    }
  }

  private toListItem(doc: StoredDocument): DocumentListItem {
    const activeJob = [...this.jobs.values()].find(
      (job) =>
        job.documentId === doc.id &&
        (job.state === 'pending' || job.state === 'running' || job.state === 'retry_wait'),
    );
    return {
      id: doc.id,
      document_version_id: doc.activeVersionId ?? '',
      version: doc.version,
      name: doc.name,
      media_kind: doc.mediaKind,
      version_status: 'active',
      active_operation:
        activeJob === undefined
          ? null
          : { job_id: activeJob.jobId, operation: activeJob.kind, state: activeJob.state },
      uploaded_at: doc.uploadedAt,
      usage: doc.usage,
    };
  }

  private toJob(job: StoredJob, canReplay = false): IngestionJob {
    return {
      job_id: job.jobId,
      document_id: job.documentId,
      name: job.name,
      space_id: job.spaceId,
      upload_batch_id: job.uploadBatchId,
      state: job.state,
      stage: job.stage,
      progress_text_hint: null,
      next_attempt_at: job.nextAttemptAt,
      usage: job.usage,
      failure_reason: job.failureReason,
      allowed_actions: allowedActionsFor(job, { canReplay }),
      ocr_low_confidence: job.ocrLowConfidence,
      notification_event_ids: [...job.notificationEventIds],
      created_at: job.createdAt,
    };
  }

  private toSubmission(submission: StoredSubmission): Submission {
    return {
      submission_id: submission.submissionId,
      space_id: submission.targetSpaceId,
      version: submission.version,
      status: submission.status,
      file_name: submission.name,
      media_kind: submission.mediaKind,
      submitter_name: submission.submitterName,
      submitter_department: submission.submitterDepartment,
      file_size: submission.sizeBytes,
      space_name: submission.targetSpaceName,
      created_at: submission.createdAt,
      reviewed_at: submission.reviewedAt,
    };
  }

  private job(jobId: string): StoredJob {
    // cancel/replay 双池查找：上传结果层任务与运维任务队列任务共用 §6.7 端点。
    const job = this.jobs.get(jobId) ?? this.opsJobs.get(jobId);
    if (job === undefined) {
      throw new MockHttpError(404, 'ingestion_job_not_found');
    }
    return job;
  }

  private document(documentId: string): StoredDocument {
    for (const list of this.documents.values()) {
      const doc = list.find((candidate) => candidate.id === documentId);
      if (doc !== undefined) {
        return doc;
      }
    }
    throw new MockHttpError(404, 'document_not_found');
  }

  private submission(submissionId: string): StoredSubmission {
    const submission = this.submissions.get(submissionId);
    if (submission === undefined) {
      throw new MockHttpError(404, 'submission_not_found');
    }
    return submission;
  }

  private nextId(prefix: string): string {
    this.seq += 1;
    return `${prefix}_${this.seq.toString(36)}${Date.now().toString(36)}`;
  }
}

/** 任务 allowed_actions 动态推导（唯一依据；空数组不渲染操作区；review C13 ACL）。 */
export function allowedActionsFor(
  job: {
    readonly state: JobState;
  },
  options: { readonly canReplay: boolean } = { canReplay: false },
): JobAction[] {
  const base = (() => {
    switch (job.state) {
      case 'pending':
      case 'running':
        return ['cancel'] as JobAction[];
      case 'retry_wait':
        return ['cancel', 'replay'] as JobAction[];
      case 'failed':
      case 'dead_letter':
        return ['replay'] as JobAction[];
      default:
        return [];
    }
  })();
  // replay 仅允许契约规定的 ops/admin（运维人工重放）；普通用户/部长只见 cancel。
  if (!options.canReplay) {
    return base.filter((action) => action !== 'replay');
  }
  return base;
}
