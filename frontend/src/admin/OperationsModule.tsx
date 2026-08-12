/*
 * 系统运维（§10 任务队列 + §9.2 指标看板；运维端 §7.6 / 超管端 §7.5）。
 * - OpsJobsLayer（§10.1–10.2）：顶部四档分段（全部 / 处理中 / 待人工处理 / 超时 ↔
 *   all/active/replayable/stale，默认 all），切换重新拉取；行 = 任务类型 + 文档名、任务 ID
 *   （等宽回退字体）、进入时间、停留时长（wait_seconds 格式化）、状态标签（与 §6.6 同一映射）、
 *   操作列；超时行整行 fog-white 底 + 琥珀状态点 + 停留时长琥珀（stale 是派生标记，不替代 job
 *   状态）。操作严格按行 allowed_actions 渲染：cancel → ghost Pill 高 28 + 二次确认 → §6.7
 *   cancel（无 body 无 Idempotency-Key）；replay → ghost Pill 转 loading → §6.7 replay（新键，
 *   202）后轮询该任务到 pending/running 呈现「解析中」。服务端对超管返回空数组 → 全部行无操作时
 *   整列不渲染（数据驱动，非角色分支）；竞态 409/403 刷新该列表。
 * - 刷新：ops 轮询 5s（卸载清除；sequence fence；行增删 250ms 淡入，静默刷新保留滚动位置与
 *   当前操作）；admin 不轮询，表头右侧手动「刷新」TextLink。
 * - OperationsMetricsLayer：固定三张卡无组包，时间窗口沿用 dashboard 所选（共享 provider 状态），
 *   本层无切换控件。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../api/errors';
import { useAuthState } from '../auth/AuthProvider';
import { copy } from '../copy';
import { createIdempotencyScope, isBusinessResponse } from '../settings/idempotency';
import { useSettings } from '../settings/SettingsProvider';
import {
  ConfirmDialog,
  CountBadge,
  EmptyState,
  ErrorState,
  HeaderNotice,
  LoadingCards,
  LoadingRows,
  Pill,
  SegmentedControl,
  StatusDot,
  TextLink,
} from '../ui';
import { useAdmin } from './AdminProvider';
import { DashboardCardView } from './DashboardModule';
import { formatDateTime } from './format';
import { useAdminRead } from './use-admin-read';
import type { OpsJobItem, OpsJobsResponse, OpsJobsView } from './types';

/** 任务队列轮询间隔（运维端 §7.6 持续刷新）。 */
const POLL_INTERVAL_MS = 5000;
/** replay 收敛轮询间隔（§6.7：202 后轮询到 pending/running）。 */
const REPLAY_CONVERGENCE_MS = 1000;
/** replay 收敛最大尝试次数（防无限轮询；到点按服务端刷新后的状态呈现）。 */
const REPLAY_CONVERGENCE_ATTEMPTS = 15;
/** 行移除过渡时长（§10.1：行增删 250ms 高度过渡 + 淡出，与 --duration-base 一致）。 */
const ROW_EXIT_MS = 250;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

const TERMINAL_STATES: ReadonlySet<string> = new Set([
  'succeeded',
  'failed',
  'dead_letter',
  'cancelled',
]);

/* ---------- 任务队列（§10.1–10.2） ---------- */

export function OpsJobsLayer() {
  const { api } = useAdmin();
  const { api: settingsApi } = useSettings();
  const { user } = useAuthState();
  // 刷新行为按端约定：ops 轮询 5s；admin 手动「刷新」文字链（《运维端设计.md》§7.6 /《超管端设计.md》§7.5）
  const isOps = user?.role === 'ops';
  const [view, setView] = useState<OpsJobsView>('all');
  const [data, setData] = useState<OpsJobsResponse | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [pendingCancel, setPendingCancel] = useState<OpsJobItem | null>(null);
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  const [busyJobIds, setBusyJobIds] = useState<readonly string[]>([]);
  const [replayingJobIds, setReplayingJobIds] = useState<readonly string[]>([]);
  // 轮询行移除：250ms 淡出 + 高度收起后再卸载（§10.1 行增删过渡，不打断滚动与操作）
  const [leavingItems, setLeavingItems] = useState<readonly OpsJobItem[]>([]);
  // 读序列代际（sequence fence）：视图切换 / 卸载推进，过期响应一律作废
  const generationRef = useRef(0);
  const viewRef = useRef(view);
  viewRef.current = view;
  const dataRef = useRef<OpsJobsResponse | null>(null);
  dataRef.current = data;
  // 取消操作 token：确认 A 飞行中关闭后打开 B，A completion 不得关 B / 写错状态
  const cancelTokenRef = useRef(0);
  const replayIdem = useRef(createIdempotencyScope());
  const copyOperations = copy.admin.operations;
  const copyUploads = copy.settings.knowledge.uploads;

  /** 静默读：成功替换数据（keyed 行复用，不打断滚动与操作）；失败时有数据则保留旧数据给行内错误行。 */
  const loadJobs = useCallback(async (): Promise<OpsJobsResponse | null> => {
    const gen = generationRef.current;
    try {
      const response = await api.listOpsJobs(viewRef.current);
      if (gen !== generationRef.current) {
        return null;
      }
      const previous = dataRef.current;
      if (previous !== null) {
        const nextIds = new Set(response.items.map((job) => job.job_id));
        // 已重新出现的离开行立即清掉，避免与当前数据重复 key
        setLeavingItems((current) => current.filter((job) => !nextIds.has(job.job_id)));
        const exited = previous.items.filter((job) => !nextIds.has(job.job_id));
        if (exited.length > 0) {
          // 离开行保留 250ms 播放淡出 + 高度收起，之后卸载（generation fence 防过期定时器）
          setLeavingItems((current) => [...current, ...exited]);
          window.setTimeout(() => {
            if (gen !== generationRef.current) {
              return;
            }
            setLeavingItems((current) =>
              current.filter((job) => !exited.some((gone) => gone.job_id === job.job_id)),
            );
          }, ROW_EXIT_MS);
        }
      }
      setData(response);
      setLoadFailed(false);
      return response;
    } catch {
      if (gen !== generationRef.current) {
        return null;
      }
      // 初载失败 → 整层错误态；静默刷新失败 → 保留旧数据 + 行内错误行（状态保持服务端刷新后的值）
      setLoadFailed(true);
      return null;
    }
  }, [api]);

  // 首次加载 + ops 轮询 5s（视图切换重置整层并重新拉取；卸载 / 切换清除定时器并作废旧响应）
  useEffect(() => {
    const gen = ++generationRef.current;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setData(null);
    setInitialLoading(true);
    setLoadFailed(false);
    // 视图切换/重挂载：清空离开行残留，避免新视图数据与其重复 key（Verifier 风险 1）
    setLeavingItems([]);
    const tick = async () => {
      await loadJobs();
      if (cancelled || gen !== generationRef.current) {
        return;
      }
      setInitialLoading(false);
      if (isOps) {
        timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
      }
    };
    void tick();
    return () => {
      cancelled = true;
      generationRef.current += 1;
      if (timer !== undefined) {
        clearTimeout(timer);
      }
    };
  }, [loadJobs, isOps, view]);

  /** 初载失败重试：补一次读（轮询由 effect 管，重试成功即恢复链）。 */
  async function retryInitial(): Promise<void> {
    setInitialLoading(true);
    setLoadFailed(false);
    await loadJobs();
    setInitialLoading(false);
  }

  /** replay 收敛轮询：针对该任务轮询到 pending/running（或明确终态）才结束（§6.7 呈现「解析中」）。 */
  async function waitForReplayConvergence(jobId: string): Promise<void> {
    const gen = generationRef.current;
    for (let attempt = 0; attempt < REPLAY_CONVERGENCE_ATTEMPTS; attempt += 1) {
      const response = await loadJobs();
      if (gen !== generationRef.current) {
        return; // 视图切换 / 卸载：放弃收敛
      }
      const current = (response ?? dataRef.current)?.items.find((item) => item.job_id === jobId);
      if (current === undefined) {
        await sleep(REPLAY_CONVERGENCE_MS);
        continue;
      }
      if (
        current.state === 'pending' ||
        current.state === 'running' ||
        TERMINAL_STATES.has(current.state)
      ) {
        return;
      }
      await sleep(REPLAY_CONVERGENCE_MS);
    }
  }

  async function confirmCancel(): Promise<void> {
    const job = pendingCancel;
    if (job === null || confirmingCancel) {
      return;
    }
    const token = cancelTokenRef.current;
    setConfirmingCancel(true);
    setNotice(null);
    try {
      // §6.7：cancel 无 body 无 Idempotency-Key
      await settingsApi.cancelJob(job.job_id, '');
      if (token !== cancelTokenRef.current) {
        return;
      }
      setPendingCancel(null);
      await loadJobs();
    } catch (error) {
      if (token !== cancelTokenRef.current) {
        return;
      }
      if (error instanceof ApiError && (error.status === 409 || error.status === 403)) {
        // 竞态：关闭对话框 + 刷新列表，按刷新后的 allowed_actions 重新渲染
        setPendingCancel(null);
        setNotice(copyUploads.actionConflict);
        await loadJobs();
      } else {
        // 其余错误：对话框保持打开，可重试或取消
        setNotice(copyOperations.actionError);
      }
    } finally {
      if (token === cancelTokenRef.current) {
        setConfirmingCancel(false);
      }
    }
  }

  async function replay(job: OpsJobItem): Promise<void> {
    if (busyJobIds.includes(job.job_id)) {
      return; // per-row busy 防重入
    }
    setBusyJobIds((ids) => [...ids, job.job_id]);
    setNotice(null);
    const key = replayIdem.current.keyFor('job-replay', job.job_id, 'replay');
    try {
      await settingsApi.replayJob(job.job_id, key);
      replayIdem.current.clear();
      // 202 后保持按钮 loading + 行内「解析中」，轮询到 pending/running 才收敛（§6.7）
      setReplayingJobIds((ids) => (ids.includes(job.job_id) ? ids : [...ids, job.job_id]));
      await waitForReplayConvergence(job.job_id);
    } catch (error) {
      if (isBusinessResponse(error)) {
        replayIdem.current.businessResponse();
      }
      if (error instanceof ApiError && (error.status === 409 || error.status === 403)) {
        setNotice(copyUploads.actionConflict);
        await loadJobs();
      } else {
        setNotice(copyOperations.actionError);
      }
    } finally {
      setBusyJobIds((ids) => ids.filter((id) => id !== job.job_id));
      setReplayingJobIds((ids) => ids.filter((id) => id !== job.job_id));
    }
  }

  // 操作列整列不渲染：数据驱动（全部行 allowed_actions 为空，如超管只读视图），非角色分支
  const showActions = data !== null && data.items.some((job) => job.allowed_actions.length > 0);
  const gridColumns = showActions
    ? 'grid-cols-[minmax(0,1fr)_auto_auto_auto_auto_auto]'
    : 'grid-cols-[minmax(0,1fr)_auto_auto_auto_auto]';
  // 渲染列表 = 当前数据 + 离开中的行（250ms 淡出收起后由定时器卸载）
  const leavingIds = new Set(leavingItems.map((job) => job.job_id));
  const visibleItems = [...(data?.items ?? []), ...leavingItems];

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-[20px] font-medium text-ink-black">
          <span className="inline-flex items-center gap-2">
            {copyOperations.jobs}
            {data !== null && <CountBadge count={data.stale_count} />}
          </span>
        </h2>
        <div className="flex items-center gap-4">
          <SegmentedControl
            options={[
              { value: 'all', label: copyOperations.viewAll },
              { value: 'active', label: copyOperations.viewActive },
              { value: 'replayable', label: copyOperations.viewReplayable },
              { value: 'stale', label: copyOperations.viewStale },
            ]}
            value={view}
            onChange={(value) => setView(value as OpsJobsView)}
            ariaLabel={copyOperations.jobs}
          />
          {!isOps && (
            <TextLink className="text-ink-black" onClick={() => void loadJobs()}>
              {copy.admin.common.refresh}
            </TextLink>
          )}
        </div>
      </div>
      {notice !== null && <HeaderNotice message={notice} onDismiss={() => setNotice(null)} />}
      {initialLoading && <LoadingRows count={3} />}
      {!initialLoading && loadFailed && data === null && (
        <ErrorState text={copyOperations.loadError} onRetry={() => void retryInitial()} />
      )}
      {!initialLoading && data !== null && (
        <>
          {loadFailed && (
            <ErrorState text={copyOperations.loadError} onRetry={() => void loadJobs()} />
          )}
          {visibleItems.length === 0 ? (
            <EmptyState text={copyOperations.emptyJobs} />
          ) : (
            <ul className="flex flex-col rounded-[var(--radius-cards)] border border-[var(--color-hairline)] bg-paper-white px-0 py-1">
              {visibleItems.map((job) => (
                <li
                  key={job.job_id}
                  className={
                    `${leavingIds.has(job.job_id) ? 'ui-row-exit' : 'ui-row-enter'} grid items-center gap-x-4 border-t border-[var(--color-hairline)] ` +
                    `px-4 py-3 first:border-t-0 ${gridColumns} ${job.stale ? 'bg-fog-white' : ''}`
                  }
                >
                  <span className="min-w-0 truncate text-[15px] text-ink-black">
                    {copyOperations.taskTypeIngestion} · {job.document_name}
                  </span>
                  <span className="font-mono text-[15px] text-ash-gray">{job.job_id}</span>
                  <span className="text-[15px] text-slate-gray">
                    {formatDateTime(job.enqueued_at)}
                  </span>
                  <span className={`text-[15px] ${job.stale ? 'text-warning' : 'text-slate-gray'}`}>
                    {copyOperations.waitDuration(job.wait_seconds)}
                  </span>
                  <span className="flex items-center gap-1.5">
                    {/* stale 是派生标记：琥珀点 + 停留时长琥珀，状态标签仍显示原 state */}
                    {job.stale && <StatusDot intent="warning" />}
                    <span className="rounded-[var(--radius-buttons)] bg-mist-gray px-2 py-1 text-caption text-ink-black">
                      {copyUploads.stateLabel(job.state)}
                    </span>
                    {replayingJobIds.includes(job.job_id) && (
                      <span className="text-[15px] text-slate-gray">
                        {copyUploads.stage.parsing}
                      </span>
                    )}
                  </span>
                  {showActions && (
                    <span className="flex items-center justify-end gap-2">
                      {job.allowed_actions.includes('cancel') && (
                        <Pill
                          variant="ghost"
                          size="xs"
                          disabled={confirmingCancel && pendingCancel?.job_id === job.job_id}
                          onClick={() => setPendingCancel(job)}
                        >
                          {copyUploads.cancel}
                        </Pill>
                      )}
                      {job.allowed_actions.includes('replay') && (
                        <Pill
                          variant="ghost"
                          size="xs"
                          loading={busyJobIds.includes(job.job_id)}
                          onClick={() => void replay(job)}
                        >
                          {copyUploads.replay}
                        </Pill>
                      )}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      <ConfirmDialog
        open={pendingCancel !== null}
        confirming={confirmingCancel}
        danger
        onOpenChange={(open) => {
          if (!open) {
            cancelTokenRef.current += 1;
            setConfirmingCancel(false);
            setPendingCancel(null);
          }
        }}
        title={copyUploads.cancelConfirmTitle}
        description={copyUploads.cancelConfirmDescription(pendingCancel?.document_name ?? '')}
        confirmLabel={copyUploads.cancel}
        onConfirm={() => void confirmCancel()}
      />
    </div>
  );
}

/* ---------- 指标看板（§9.2） ---------- */

export function OperationsMetricsLayer() {
  // 时间窗口沿用 dashboard 所选（共享 provider 状态），本层不重复切换控件。
  const { api, metricsWindow } = useAdmin();
  const read = useAdminRead(() => api.getOperationsMetrics(metricsWindow), [api, metricsWindow]);
  const copyOperations = copy.admin.operations;
  const copyDashboard = copy.admin.dashboard;
  // 窗口在 dashboard 侧切换后回到本层时整层重取；刷新中保留旧卡（数值区交叉淡变复用卡视图）。
  const switching = read.loading && read.data !== null;

  return (
    <div className="flex flex-col gap-4">
      <h2 className="text-[20px] font-medium text-ink-black">{copyOperations.metrics}</h2>
      {read.data === null && read.loading && <LoadingCards count={2} />}
      {read.data === null && read.error && (
        <ErrorState text={copyOperations.loadError} onRetry={read.reload} />
      )}
      {read.data !== null &&
        (read.data.cards.length === 0 ? (
          <EmptyState text={copyDashboard.empty} />
        ) : (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-4">
            {read.data.cards.map((card, index) => (
              <DashboardCardView
                key={card.key}
                card={card}
                index={index}
                switching={switching}
                error={read.error}
                onRetry={read.reload}
              />
            ))}
          </div>
        ))}
    </div>
  );
}
