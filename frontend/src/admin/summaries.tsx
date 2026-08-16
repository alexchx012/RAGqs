/*
 * 抽屉左栏项右侧摘要（spec §1；《运维端设计.md》§7.1、《超管端设计.md》§7.1 摘要列）。
 * - 审批中心与其下钻项：待处理计数徽标（GET /approvals/summary，为 0 不显示）；
 * - 评测与校准：窗口状态点（GET /calibration/window，开窗中 = 成功绿脉冲，其余不显示）；
 * - 系统运维：超时任务计数徽标（GET /ops/jobs?view=stale 的 stale_count >0 警告琥珀 pill）。
 * 各组件挂载自加载 + AdminProvider.summariesVersion 变化重取（写操作后 invalidateSummaries）；
 * 加载中 / 出错 / 无需展示时一律渲染 null（静默，不打断左栏）；读序列带 generation fence。
 */

import { useEffect, useRef, useState } from 'react';
import { CountBadge, StatusDot } from '../ui';
import type { AdminApi } from './api';
import { useAdmin } from './AdminProvider';

/** 摘要读：挂载与 summariesVersion 变化时重取；失败静默（null）；过期响应经 generation fence 作废。 */
function useSummaryValue<T>(fetcher: (api: AdminApi) => Promise<T>): T | null {
  const { api, summariesVersion } = useAdmin();
  const [value, setValue] = useState<T | null>(null);
  const generationRef = useRef(0);

  useEffect(() => {
    const generation = ++generationRef.current;
    void fetcher(api).then(
      (result) => {
        if (generationRef.current === generation) {
          setValue(result);
        }
      },
      () => {
        // 静默失败：摘要不渲染任何内容
        if (generationRef.current === generation) {
          setValue(null);
        }
      },
    );
    return () => {
      // 依赖变化 / 卸载：作废旧响应
      generationRef.current += 1;
    };
  }, [api, summariesVersion, fetcher]);

  return value;
}

/* fetcher 一律模块级定义：引用稳定，不随渲染重复触发请求。 */
const fetchQuotaPending = (api: AdminApi): Promise<number> =>
  api.getApprovalSummary().then((summary) => summary.quota_pending);
const fetchCalibrationStatus = (api: AdminApi) =>
  api.getCalibrationWindow().then((window) => window.status);
const fetchStaleCount = (api: AdminApi): Promise<number> =>
  api.listOpsJobs('stale').then((response) => response.stale_count);

/** 审批中心模块项：可靠的配额待处理徽标（仅 ops 挂载；为 0 不显示）。 */
export function ApprovalsSummaryBadge() {
  const pending = useSummaryValue(fetchQuotaPending);
  if (pending === null || pending <= 0) {
    return null;
  }
  return <CountBadge count={pending} />;
}

/** 「配额申请」下钻项：quota_pending 徽标（为 0 不显示）。 */
export function QuotaRequestsSummaryBadge() {
  const pending = useSummaryValue(fetchQuotaPending);
  if (pending === null || pending <= 0) {
    return null;
  }
  return <CountBadge count={pending} />;
}

/** 评测与校准模块项：校准窗口状态点（open = 成功绿脉冲；closing / closed 不显示）。 */
export function EvaluationWindowDot() {
  const status = useSummaryValue(fetchCalibrationStatus);
  if (status !== 'open') {
    return null;
  }
  return <StatusDot intent="success" pulse />;
}

/** 系统运维模块项：超时任务计数徽标（stale_count >0 警告琥珀 pill；为 0 不显示）。 */
export function OperationsStaleBadge() {
  const stale = useSummaryValue(fetchStaleCount);
  if (stale === null || stale <= 0) {
    return null;
  }
  return (
    <span className="inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-[var(--radius-buttons)] bg-warning/15 px-1.5 text-[12px] font-w480 text-warning">
      {stale}
    </span>
  );
}
