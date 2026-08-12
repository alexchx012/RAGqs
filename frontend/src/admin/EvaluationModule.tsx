/*
 * 评测与校准（§11；运维端 §7.5 / 超管端 §7.4）：同页三区块不下钻（间距 32px、区块标题 20px 500）。
 * - 校准窗口状态卡（§11.2–11.3）：6px 状态点（open = 成功绿脉冲；closing / closed = slate）+
 *   窗口起止时间 / 已收集对比数 / 实际采样率（0–1 格式化百分比）/ 生效策略版本；无窗口按服务端
 *   合成 closed 读模型展示；closing 继续展示收口倒计时（close_deadline_at）；前端不根据排行榜
 *   差距自行改变状态。系统建议开窗经铃铛送达，面板不自动开窗（无相关入口）。
 * - 开关仅 ops：拨动即弹确认对话框（open 必填 window_kind，对话框内 cold_start/sentinel/manual
 *   单选），确认后才 POST /calibration/window + Idempotency-Key；取消对话框 switch 回原位
 *   （受控，不乐观切换）；409 四码对话框内错误行说明 + 重新拉 window 按服务端状态刷新；
 *   成功后 invalidateSummaries（左栏状态点联动）。admin 不渲染开关与任何操作按钮，
 *   仅状态文字 + 「开窗由运维操作」说明行。
 * - 排行榜 / 影子评测排名（§11.1 同表同规格两区块）：metrics map 动态列（键集并集、稳定排序）；
 *   is_active 当前生效行底 fog-white；名次无奖牌彩色；eligible=false 名称后行内说明；
 *   policy 只读行是当前视图唯一策略数值来源（阈值 / 采样率 / 上下限，无页面内覆盖入口）。
 * - 影子评测触发（§11.4）不在前端调用或渲染（非目标）。
 */

import { useRef, useState } from 'react';
import { ApiError } from '../api/errors';
import { useAuthState } from '../auth/AuthProvider';
import { copy } from '../copy';
import { createIdempotencyScope, isBusinessResponse } from '../settings/idempotency';
import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  HeaderNotice,
  LoadingRows,
  SegmentedControl,
  StatusDot,
  Switch,
} from '../ui';
import { useAdmin } from './AdminProvider';
import { formatDateTime, formatPercent, formatTime } from './format';
import { useAdminRead } from './use-admin-read';
import type {
  CalibrationWindowAction,
  CalibrationWindowKind,
  CalibrationWindowStatus,
  EvaluationPolicy,
  LeaderboardEntry,
} from './types';

function windowStatusLabel(status: CalibrationWindowStatus): string {
  const evaluation = copy.admin.evaluation;
  switch (status) {
    case 'open':
      return evaluation.statusOpen;
    case 'closing':
      return evaluation.statusClosing;
    default:
      return evaluation.statusClosed;
  }
}

function kindLabel(kind: CalibrationWindowKind | null): string | null {
  const evaluation = copy.admin.evaluation;
  switch (kind) {
    case 'cold_start':
      return evaluation.kindColdStart;
    case 'sentinel':
      return evaluation.kindSentinel;
    case 'manual':
      return evaluation.kindManual;
    default:
      return null;
  }
}

/** 409 四码 → 对话框内错误行；其余业务 / 网络错误走兜底措辞。 */
function windowActionErrorMessage(error: unknown): string {
  const evaluation = copy.admin.evaluation;
  if (error instanceof ApiError && error.status === 409) {
    switch (error.code) {
      case 'calibration_window_not_eligible':
        return evaluation.errorNotEligible;
      case 'calibration_window_already_open':
        return evaluation.errorAlreadyOpen;
      case 'calibration_window_closing':
        return evaluation.errorClosing;
      case 'calibration_window_not_open':
        return evaluation.errorNotOpen;
      default:
    }
  }
  return evaluation.actionError;
}

/* ---------- 校准窗口状态卡（§11.2–11.3） ---------- */

function CalibrationWindowCard() {
  const { api, invalidateSummaries } = useAdmin();
  const { user } = useAuthState();
  const isOps = user?.role === 'ops';
  const read = useAdminRead(() => api.getCalibrationWindow(), [api]);
  const [pendingAction, setPendingAction] = useState<CalibrationWindowAction | null>(null);
  const [kind, setKind] = useState<CalibrationWindowKind>('cold_start');
  const [confirming, setConfirming] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const idem = useRef(createIdempotencyScope());
  const evaluation = copy.admin.evaluation;

  function closeDialog(): void {
    // 取消 / 关闭对话框：清键（下次确认拿新键），switch 受控保持服务端状态
    idem.current.clear();
    setDialogError(null);
    setPendingAction(null);
  }

  async function confirm(): Promise<void> {
    const action = pendingAction;
    if (action === null || confirming) {
      return;
    }
    setConfirming(true);
    setDialogError(null);
    const key = idem.current.keyFor(
      'calibration-window',
      'singleton',
      `${action}:${action === 'open' ? kind : ''}`,
    );
    try {
      await api.postCalibrationWindow(action, action === 'open' ? kind : null, key);
      idem.current.clear();
      setPendingAction(null);
      setNotice(action === 'open' ? evaluation.openedNotice : evaluation.closingNotice);
      invalidateSummaries();
      read.reload();
    } catch (error) {
      if (isBusinessResponse(error)) {
        idem.current.businessResponse();
      }
      setDialogError(windowActionErrorMessage(error));
      // 409 / 403 一律按服务端状态刷新（不在客户端伪造状态转换）
      read.reload();
    } finally {
      setConfirming(false);
    }
  }

  const window = read.data;

  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-[20px] font-medium text-ink-black">{evaluation.windowCardTitle}</h2>
      {notice !== null && (
        <HeaderNotice message={notice} intent="success" onDismiss={() => setNotice(null)} />
      )}
      <div className="rounded-[var(--radius-cards)] border border-[var(--color-hairline)] bg-paper-white p-5">
        {read.loading && <LoadingRows count={1} />}
        {read.error && <ErrorState text={evaluation.windowLoadError} onRetry={read.reload} />}
        {!read.loading && !read.error && window !== null && (
          <div className="flex flex-col gap-3">
            {/* 状态行交叉淡变 250ms：状态切换 keyed 重挂载播 ui-fade-in */}
            <div
              key={window.status}
              className="ui-fade-enter flex items-center gap-2 text-[15px] text-ink-black"
            >
              <StatusDot
                intent={window.status === 'open' ? 'success' : 'slate'}
                pulse={window.status === 'open'}
              />
              <span>{windowStatusLabel(window.status)}</span>
              {kindLabel(window.window_kind) !== null && (
                <span className="text-slate-gray">· {kindLabel(window.window_kind)}</span>
              )}
            </div>
            <div className="flex flex-col gap-1 text-[15px] text-slate-gray">
              {window.opened_at !== null && window.closed_at !== null ? (
                <p>{evaluation.windowRange(formatDateTime(window.opened_at), formatDateTime(window.closed_at))}</p>
              ) : window.opened_at !== null ? (
                <p>{evaluation.windowOpenedAt(formatDateTime(window.opened_at))}</p>
              ) : null}
              <p>
                {evaluation.pairsCollected(window.pairs_collected)}
                {' · '}
                {evaluation.sampleRate(formatPercent(window.sample_rate))}
                {window.policy_version !== null && ` · ${evaluation.policyVersion(window.policy_version)}`}
              </p>
              {window.status === 'closing' && window.close_deadline_at !== null && (
                <p>{evaluation.closingDeadline(formatTime(window.close_deadline_at))}</p>
              )}
            </div>
            {isOps ? (
              <div className="flex items-center gap-3">
                <Switch
                  checked={window.status === 'open'}
                  disabled={confirming}
                  onCheckedChange={(next) => {
                    setDialogError(null);
                    setPendingAction(next ? 'open' : 'close');
                  }}
                  ariaLabel={evaluation.switchAria}
                />
                <span className="text-[15px] text-slate-gray">
                  {window.status === 'open' ? evaluation.close : evaluation.open}
                </span>
              </div>
            ) : (
              <p className="text-[15px] text-slate-gray">{evaluation.opsOnlyNote}</p>
            )}
          </div>
        )}
      </div>
      <ConfirmDialog
        open={pendingAction !== null}
        confirming={confirming}
        onOpenChange={(open) => {
          if (!open) {
            closeDialog();
          }
        }}
        title={pendingAction === 'close' ? evaluation.closeDialogTitle : evaluation.openDialogTitle}
        description={
          pendingAction === 'close'
            ? evaluation.closeDialogDescription
            : evaluation.openDialogDescription
        }
        onConfirm={() => void confirm()}
      >
        {pendingAction === 'open' && (
          <div className="flex flex-col gap-2">
            <span className="text-[15px] text-slate-gray">{evaluation.kindLabel}</span>
            <SegmentedControl
              options={[
                { value: 'cold_start', label: evaluation.kindColdStart },
                { value: 'sentinel', label: evaluation.kindSentinel },
                { value: 'manual', label: evaluation.kindManual },
              ]}
              value={kind}
              onChange={(value) => {
                if (!confirming) {
                  setKind(value as CalibrationWindowKind);
                }
              }}
              ariaLabel={evaluation.kindLabel}
            />
          </div>
        )}
        {dialogError !== null && <p className="text-[15px] text-danger">{dialogError}</p>}
      </ConfirmDialog>
    </section>
  );
}

/* ---------- 排行榜 / 影子评测排名（§11.1 同表同规格） ---------- */

function LeaderboardTable({ entries }: { readonly entries: readonly LeaderboardEntry[] }) {
  const evaluation = copy.admin.evaluation;
  // metrics map 动态列：键集取全部行并集，列序稳定排序
  const metricKeys = [...new Set(entries.flatMap((entry) => Object.keys(entry.metrics)))].sort();
  return (
    <table className="w-full border-collapse text-left">
      <thead>
        <tr className="text-[14px] text-ash-gray">
          <th className="px-4 py-2 font-normal">{evaluation.colRank}</th>
          <th className="px-4 py-2 font-normal">{evaluation.colName}</th>
          <th className="px-4 py-2 font-normal">{evaluation.colScore}</th>
          {metricKeys.map((key) => (
            <th key={key} className="px-4 py-2 font-normal">
              {key}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {entries.map((entry) => (
          <tr
            key={`${entry.rank}:${entry.name}`}
            className={`border-t border-[var(--color-hairline)] ${entry.is_active ? 'bg-fog-white' : ''}`}
          >
            {/* 名次 Sohne 500 16px ink，不用奖牌彩色 */}
            <td className="px-4 py-3 text-[16px] font-medium text-ink-black">{entry.rank}</td>
            <td className="px-4 py-3 text-[15px] text-ink-black">
              {entry.name}
              {!entry.eligible && (
                <span className="ml-2 text-[14px] text-smoke-gray">{evaluation.notEligibleTag}</span>
              )}
            </td>
            <td className="px-4 py-3 text-[15px] text-slate-gray">{entry.score}</td>
            {metricKeys.map((key) => (
              <td key={key} className="px-4 py-3 text-[15px] text-slate-gray">
                {entry.metrics[key] ?? '—'}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function LeaderboardCard({ entries }: { readonly entries: readonly LeaderboardEntry[] }) {
  const evaluation = copy.admin.evaluation;
  return (
    <div className="rounded-[var(--radius-cards)] border border-[var(--color-hairline)] bg-paper-white p-2">
      {entries.length === 0 ? (
        <EmptyState text={evaluation.empty} />
      ) : (
        <LeaderboardTable entries={entries} />
      )}
    </div>
  );
}

/** policy 只读行：当前视图唯一策略数值来源（阈值 / 采样率 / 上下限），不提供页面内覆盖入口。 */
function policyLine(policy: EvaluationPolicy): string {
  const evaluation = copy.admin.evaluation;
  return [
    evaluation.policyVersion(policy.policy_version),
    evaluation.policyGap(policy.calibration_open_score_gap),
    evaluation.policyColdStartRate(formatPercent(policy.cold_start_sample_rate)),
    evaluation.policySentinelRate(formatPercent(policy.sentinel_sample_rate)),
    evaluation.policyMinRealQueries(policy.min_real_queries),
    evaluation.policyShadowMaxExamples(policy.shadow_max_examples),
    evaluation.policyShadowMaxConfigs(policy.shadow_max_candidate_configs),
  ].join(' · ');
}

export function EvaluationModule() {
  const { api } = useAdmin();
  const read = useAdminRead(() => api.getLeaderboard(), [api]);
  const evaluation = copy.admin.evaluation;

  return (
    <div className="flex flex-col gap-8">
      <CalibrationWindowCard />
      {read.error ? (
        // 排行榜 + 影子排名同一读接口：失败给一条错误行 + 重试（窗口卡读接口独立自管）
        <ErrorState text={evaluation.loadError} onRetry={read.reload} />
      ) : (
        <>
          <section className="flex flex-col gap-3">
            <h2 className="text-[20px] font-medium text-ink-black">{evaluation.leaderboardTitle}</h2>
            {read.loading || read.data === null ? (
              <LoadingRows count={3} />
            ) : (
              <>
                <LeaderboardCard entries={read.data.entries} />
                <p className="px-1 text-[15px] text-slate-gray">{policyLine(read.data.policy)}</p>
              </>
            )}
          </section>
          <section className="flex flex-col gap-3">
            <h2 className="text-[20px] font-medium text-ink-black">{evaluation.shadowTitle}</h2>
            {read.loading || read.data === null ? (
              <LoadingRows count={2} />
            ) : (
              <LeaderboardCard entries={read.data.shadow_entries} />
            )}
          </section>
        </>
      )}
    </div>
  );
}
