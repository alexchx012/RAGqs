/*
 * 上传结果层（settings-personal §6，知识库模块下钻子界面）。
 * - 任务卡列表：GET /ingestion-jobs 轮询；has_more 窗口提示；stage 仅 pending/running 显示
 *   「排队中/解析中/索引中」，不做假进度；retry_wait 显示 next_attempt_at；succeeded 显示用量；
 *   failed/dead_letter 显示 failure_reason；ocr_low_confidence 琥珀标记。
 * - 批次汇总（仅直接入库多文件）：GET /upload-batches/{id}，partial 仅作批次标题/汇总提示，
 *   单卡只读任务 state；前端不自行汇总。
 * - 操作区严格按 allowed_actions 渲染（cancel 二次确认；replay 加载态后轮询到 pending/running
 *   呈现「解析中」）；空数组不渲染；竞态 409/403 刷新该卡按新数组重渲染。
 * - ack 时序：仅当 succeeded 任务的 notification_event_ids 非空、且其用量/低置信信息已经真实
 *   commit 渲染（render effect 登记已渲染事件 ID）后才逐个 ack；离开本层不 ack；已 ack 去重；
 *   ack 成功后按既有轮询刷新未读数。
 * - 上传结果历史（Major3）：本层顶部呈现最近一次上传响应的逐文件结果（会话内存档）。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useAuthState, useAuthStore } from '../auth/AuthProvider';
import { useSettings } from './SettingsProvider';
import { ApiError } from '../api/errors';
import { copy } from '../copy';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import { EmptyState, ErrorState, LoadingCards } from '../ui/states';
import { Pill } from '../ui/Pill';
import type { IngestionJob } from './types';
import { UploadHistorySection } from './UploadHistory';
import { createIdempotencyScope, isBusinessResponse } from './idempotency';

const POLL_INTERVAL_MS = 2000;
const JOB_LIMIT = 50;

function stageLabel(stage: string | null): string | null {
  switch (stage) {
    case 'queued':
      return copy.settings.knowledge.uploads.stage.queued;
    case 'parsing':
      return copy.settings.knowledge.uploads.stage.parsing;
    case 'indexing':
      return copy.settings.knowledge.uploads.stage.indexing;
    default:
      return null;
  }
}

function formatDateTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString('zh-CN');
}

export function UploadsLayer() {
  const { api, notifications } = useSettings();
  const { user } = useAuthState();
  const authStore = useAuthStore();
  const sessionKey = user !== null && authStore.getAuthSessionId() !== null ? `${authStore.getAuthSessionId()}:${user.id}` : null;
  const [jobs, setJobs] = useState<readonly IngestionJob[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [batchSummaries, setBatchSummaries] = useState<Map<string, string>>(new Map());
  const [pendingCancel, setPendingCancel] = useState<IngestionJob | null>(null);
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  const [replayingJobId, setReplayingJobId] = useState<string | null>(null);
  // 取消操作 token（review A3）：确认 A 飞行中关闭后打开 B，A completion 不得关 B/写错状态
  const cancelTokenRef = useRef(0);
  const invalidateCancelOperation = () => {
    cancelTokenRef.current += 1;
  };
  const [actionError, setActionError] = useState<string | null>(null);
  const replayIdem = useRef(createIdempotencyScope());
  // 空间 ID → 名称（上传结果显示空间名而非内部 ID）
  const [spaceNames, setSpaceNames] = useState<Map<string, string>>(new Map());
  // view/session generation（review Major 1）：loadJobs 内每个 setState 只允许当前代写入；
  // 会话切换/离开层时递增使旧响应作废。
  const generationRef = useRef(0);
  // 最新 jobs 快照（replay 收敛轮询读取；render 期同步）。
  const jobsRef = useRef<readonly IngestionJob[]>([]);
  jobsRef.current = jobs;

  // ---- ack 时序（Major7）：仅对已真实渲染 commit 的事件 ack ----
  /** 已渲染 commit 的事件 ID（render effect 登记；空数组/渲染失败不登记）。 */
  const [renderedEventIds, setRenderedEventIds] = useState<readonly string[]>([]);
  const renderedEventIdsRef = useRef<readonly string[]>([]);
  renderedEventIdsRef.current = renderedEventIds;
  /** 已 ack 事件去重（跨轮询保留）。 */
  const ackedEventIdsRef = useRef<Set<string>>(new Set());
  /** ack 终态失败（404/409：事件不存在/不可 ack）：停止重试该事件（review Major 5）。 */
  const ackTerminalFailedRef = useRef<Set<string>>(new Set());
  const ackInFlightRef = useRef(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // 任务卡渲染后登记 succeeded 任务的 event ids（结果/用量/低置信信息已 commit 到 DOM）。
  // fail-closed（B9）：succeeded 但 usage 缺失（结果未渲染）时不登记、不 ack。
  useEffect(() => {
    const next: string[] = [];
    for (const job of jobs) {
      if (
        job.state === 'succeeded' &&
        job.usage !== null &&
        job.notification_event_ids.length > 0
      ) {
        next.push(...job.notification_event_ids);
      }
    }
    if (next.length > 0) {
      setRenderedEventIds(next);
    }
  }, [jobs]);

  // 已渲染事件的 ack：离开本层（卸载）不再 ack；去重；失败下轮重试。
  useEffect(() => {
    if (renderedEventIds.length === 0 || !mountedRef.current) {
      return;
    }
    const eligible = renderedEventIds.filter(
      (eventId) =>
        !ackedEventIdsRef.current.has(eventId) && !ackTerminalFailedRef.current.has(eventId),
    );
    if (eligible.length === 0) {
      return;
    }
    if (ackInFlightRef.current) {
      return;
    }
    ackInFlightRef.current = true;
    let cancelled = false;
    void (async () => {
      try {
        for (const eventId of eligible) {
          if (cancelled || !mountedRef.current) {
            return;
          }
          try {
            await api.ackNotification(eventId);
            ackedEventIdsRef.current.add(eventId);
          } catch (error) {
            if (error instanceof ApiError && (error.status === 404 || error.status === 409)) {
              // 终态：事件不存在或不可 ack——停止重试，避免每轮轮询重复打
              ackTerminalFailedRef.current.add(eventId);
            } else {
              // 网络/临时错：保留未 ack 状态，下轮渲染后重试
            }
          }
        }
        if (mountedRef.current && notifications !== null) {
          void notifications.refreshUnread();
        }
      } finally {
        ackInFlightRef.current = false;
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [renderedEventIds, api, notifications]);

  const loadJobs = useCallback(async () => {
    const gen = generationRef.current;
    try {
      const response = await api.listJobs({ limit: JOB_LIMIT });
      if (gen !== generationRef.current) {
        return null;
      }
      setJobs(response.items);
      setHasMore(response.has_more);
      setLoadError(false);
      // 空间名映射：任务卡显示目标空间名称而非内部 ID（同样只允许当前代写入）
      const ids = [...new Set(response.items.map((job) => job.space_id).filter((id) => id !== ''))];
      if (ids.length > 0) {
        const spaces = await api.listUploadSpaces().catch(() => null);
        if (spaces !== null && gen === generationRef.current) {
          const byId = new Map<string, string>();
          for (const space of spaces.items) {
            byId.set(space.id, space.name);
          }
          setSpaceNames(byId);
        }
      }
      return response.items;
    } catch {
      if (gen === generationRef.current) {
        setLoadError(true);
      }
      return null;
    }
  }, [api]);

  // 首次加载 + 轮询（打开页面、开始轮询本身不 ack）；会话切换时清空并重启轮询
  useEffect(() => {
    const gen = ++generationRef.current;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    // 会话切换：立即清空账号相关 state（配合 DrawerHost 重挂载；此处兜底）
    setJobs([]);
    setHasMore(false);
    setLoading(true);
    setLoadError(false);
    setBatchSummaries(new Map());
    ackedEventIdsRef.current = new Set();
    ackTerminalFailedRef.current = new Set();
    const tick = async () => {
      const items = await loadJobs();
      if (cancelled || gen !== generationRef.current) {
        return;
      }
      setLoading(false);
      if (items !== null) {
        // 批次汇总（仅直接入库多文件批次；partial 仅批次标题提示，单卡只读任务 state）
        const batchIds = [...new Set(items.map((job) => job.upload_batch_id).filter((id): id is string => id !== null))];
        if (batchIds.length > 0) {
          void Promise.all(batchIds.map((id) => api.getUploadBatch(id).catch(() => null))).then((batches) => {
            if (cancelled || gen !== generationRef.current) {
              return;
            }
            const next = new Map<string, string>();
            for (const batch of batches) {
              if (batch !== null) {
                next.set(
                  batch.upload_batch_id,
                  batch.state === 'partial'
                    ? copy.settings.knowledge.uploads.batchPartial
                    : copy.settings.knowledge.uploads.batchTitle(batch.upload_batch_id),
                );
              }
            }
            setBatchSummaries(next);
          });
        }
      }
      if (!cancelled && gen === generationRef.current) {
        timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
      }
    };
    void tick();
    return () => {
      cancelled = true;
      generationRef.current += 1; // 离开层/会话切换：作废所有在途响应
      if (timer !== undefined) {
        clearTimeout(timer);
      }
    };
  }, [loadJobs, sessionKey]);

  const cancelJob = async () => {
    const job = pendingCancel;
    if (job === null || confirmingCancel) {
      return;
    }
    const token = cancelTokenRef.current;
    setConfirmingCancel(true);
    setActionError(null);
    try {
      await api.cancelJob(job.job_id);
      if (token !== cancelTokenRef.current) {
        return; // 已关闭/打开 B：A completion no-op
      }
      setPendingCancel(null);
      await loadJobs();
    } catch (error) {
      if (token !== cancelTokenRef.current) {
        return;
      }
      if (error instanceof ApiError && (error.status === 409 || error.status === 403)) {
        setPendingCancel(null);
        setActionError(copy.settings.knowledge.uploads.actionConflict);
        await loadJobs();
      } else if (isBusinessResponse(error)) {
        setActionError(copy.settings.knowledge.manage.actionError);
      } else {
        // 网络未知/超时：保留确认对话框，由用户重试或取消
        setActionError(copy.settings.knowledge.manage.actionError);
      }
    } finally {
      if (token === cancelTokenRef.current) {
        setConfirmingCancel(false);
      }
    }
  };

  /** replay 收敛轮询：针对 job 轮询到 pending/running（含 stage）或明确终态才结束（review Major 2）。 */
  const waitForReplayConvergence = async (jobId: string): Promise<void> => {
    const gen = generationRef.current;
    const TERMINAL = new Set(['succeeded', 'failed', 'dead_letter', 'cancelled']);
    for (let attempt = 0; attempt < 15; attempt += 1) {
      await loadJobs();
      if (gen !== generationRef.current) {
        return; // 会话/视图已切换：放弃收敛
      }
      const current = jobsRef.current.find((item) => item.job_id === jobId);
      if (current === undefined) {
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        continue;
      }
      if (current.state === 'pending' || current.state === 'running' || TERMINAL.has(current.state)) {
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
    }
  };

  const replayJob = async (job: IngestionJob) => {
    if (replayingJobId !== null) {
      return;
    }
    setReplayingJobId(job.job_id);
    setActionError(null);
    const idempotencyKey = replayIdem.current.keyFor('replay-job', job.job_id, 'replay');
    try {
      const result = await api.replayJob(job.job_id, idempotencyKey);
      replayIdem.current.clear();
      // 保持 loading：针对该 job 轮询到 pending/running 或明确终态；不能一次 GET 后立即显示 replay
      await waitForReplayConvergence(result.job_id);
    } catch (error) {
      if (error instanceof ApiError && (error.status === 409 || error.status === 403)) {
        replayIdem.current.businessResponse();
        setActionError(copy.settings.knowledge.uploads.actionConflict);
        await loadJobs();
      } else if (isBusinessResponse(error)) {
        replayIdem.current.businessResponse();
        setActionError(copy.settings.knowledge.manage.actionError);
      } else {
        // 网络未知/超时：复用同键同体重试
        setActionError(copy.settings.knowledge.manage.actionError);
      }
    } finally {
      setReplayingJobId(null);
    }
  };

  return (
    <section aria-label={copy.settings.knowledge.uploads.title} className="pb-10">
      {hasMore && (
        <p className="mb-4 text-caption text-smoke-gray">{copy.settings.knowledge.uploads.recentWindow}</p>
      )}
      {/* 上传结果历史：最近一次上传响应逐文件结果（不随上传对话框卸载丢失） */}
      <UploadHistorySection sessionKey={sessionKey} />
      {loading ? (
        <LoadingCards count={2} />
      ) : loadError ? (
        <ErrorState onRetry={() => void loadJobs()} />
      ) : jobs.length === 0 ? (
        <EmptyState text={copy.settings.knowledge.uploads.empty} />
      ) : (
        <ul className="mt-4 flex flex-col gap-3">
          {jobs.map((job) => (
            <JobCard
              key={job.job_id}
              job={job}
              batchHint={job.upload_batch_id === null ? null : (batchSummaries.get(job.upload_batch_id) ?? null)}
              spaceName={job.space_id === '' ? '' : (spaceNames.get(job.space_id) ?? job.space_id)}
              replaying={replayingJobId === job.job_id}
              onCancel={() => setPendingCancel(job)}
              onReplay={() => void replayJob(job)}
            />
          ))}
        </ul>
      )}
      {actionError !== null && (
        <p role="alert" className="mt-4 text-caption text-danger">
          {actionError}
        </p>
      )}

      <ConfirmDialog
        open={pendingCancel !== null}
        confirming={confirmingCancel}
        onOpenChange={(open) => {
          if (!open) {
            invalidateCancelOperation();
            setConfirmingCancel(false);
            setPendingCancel(null);
          }
        }}
        title={copy.settings.knowledge.uploads.cancelConfirmTitle}
        description={copy.settings.knowledge.uploads.cancelConfirmDescription(pendingCancel?.name ?? '')}
        confirmLabel={copy.settings.knowledge.uploads.cancel}
        danger
        onConfirm={() => void cancelJob()}
      />
    </section>
  );
}

interface JobCardProps {
  readonly job: IngestionJob;
  readonly batchHint: string | null;
  readonly spaceName: string;
  readonly replaying: boolean;
  readonly onCancel: () => void;
  readonly onReplay: () => void;
}

function JobCard({ job, batchHint, spaceName, replaying, onCancel, onReplay }: JobCardProps) {
  const stage = job.state === 'pending' || job.state === 'running' ? stageLabel(job.stage) : null;
  const actions = job.allowed_actions;

  return (
    <li className="rounded-[var(--radius-elevatedcards)] border border-[var(--color-hairline)] p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="truncate text-body text-ink-black">{job.name}</p>
          <p className="mt-1 text-caption text-smoke-gray">
            {copy.settings.knowledge.uploads.enteringAt(formatDateTime(job.created_at))}
            {spaceName !== '' && ` · ${copy.settings.knowledge.uploads.targetSpace(spaceName)}`}
          </p>
          {batchHint !== null && <p className="mt-1 text-caption text-slate-gray">{batchHint}</p>}
        </div>
        <span className="shrink-0 rounded-[var(--radius-buttons)] bg-mist-gray px-2 py-1 text-caption text-ink-black">
          {copy.settings.knowledge.uploads.stateLabel(job.state)}
        </span>
      </div>

      {stage !== null && (
        <p className="mt-3 text-caption text-slate-gray">{stage}</p>
      )}
      {job.state === 'retry_wait' && job.next_attempt_at !== null && (
        <p className="mt-1 text-caption text-slate-gray">
          {copy.settings.knowledge.uploads.nextAttemptAt(formatDateTime(job.next_attempt_at))}
        </p>
      )}
      {job.state === 'succeeded' && job.usage !== null && (
        <p className="mt-2 text-caption text-success">
          {copy.settings.knowledge.uploads.usageSucceeded(job.usage.pages, job.usage.images)}
        </p>
      )}
      {(job.state === 'failed' || job.state === 'dead_letter') && job.failure_reason !== null && (
        <p className="mt-2 text-caption text-danger">
          {copy.settings.knowledge.uploads.failureReason(job.failure_reason)}
        </p>
      )}
      {job.ocr_low_confidence && (
        <p className="mt-2 text-caption text-warning">{copy.settings.knowledge.uploads.ocrLowConfidence}</p>
      )}

      {/* 操作区严格按 allowed_actions 渲染；空数组不渲染 */}
      {actions.length > 0 && (
        <div className="mt-3 flex items-center gap-2">
          {actions.includes('cancel') && (
            <Pill variant="ghost" size="sm" onClick={onCancel}>
              {copy.settings.knowledge.uploads.cancel}
            </Pill>
          )}
          {actions.includes('replay') && (
            <Pill variant="ghost" size="sm" loading={replaying} onClick={onReplay}>
              {copy.settings.knowledge.uploads.replay}
            </Pill>
          )}
        </div>
      )}
    </li>
  );
}
