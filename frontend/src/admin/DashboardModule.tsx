/*
 * 总览模块（§9.1；运维端 §7.2 / 超管端 §7.2）。
 * 页头：页级标题 + 时间窗口分段控件（今日 / 近 7 天 / 近 30 天，默认近 7 天）。
 * 数据 GET /metrics/dashboard?window= 由后端按角色组包（运维四包 / 超管四包），
 * 前端不判断角色差异、按 packs/cards 数据驱动渲染；卡渲染（四卡型 / 超阈 / 整卡跳转 /
 * 六态 / 切换动效）见 dashboard/DashboardCardView.tsx，指标看板（§9.2）复用同一视图。
 * 状态粒度：首载整区 LoadingCards、首载失败整区 ErrorState；窗口切换保留旧卡
 * （数值区 150ms 淡出 → 新数据 250ms 错峰 index×30ms 淡入）；切换或重试失败时旧卡
 * 保留在网格位并逐卡错误态，点重试只重置该卡（先回骨架）后整体重拉（数据单端点）。
 * 可读性：内容区根 pr-4——溢出滚动时右列卡片右缘与纵向滚动条之间保留 16px 留白
 * （纯间距无颜色，亮/暗主题均成立）。
 */

import { useCallback, useEffect, useState } from 'react';
import { copy } from '../copy';
import {
  EmptyState,
  ErrorState,
  LoadingCards,
  SegmentedControl,
  type SegmentedOption,
} from '../ui';
import { useAdmin } from './AdminProvider';
import { DashboardCardView } from './dashboard/DashboardCardView';
import { useAdminRead } from './use-admin-read';
import type { MetricsWindow } from './types';

export { DashboardCardView } from './dashboard/DashboardCardView';

/** dashboard 与指标看板共用同一组时间窗口分段（§9.1/§9.2）。 */
export const METRICS_WINDOW_OPTIONS: readonly SegmentedOption[] = [
  { value: 'today', label: copy.admin.dashboard.today },
  { value: '7d', label: copy.admin.dashboard.d7 },
  { value: '30d', label: copy.admin.dashboard.d30 },
];

export function DashboardModule() {
  const { api, metricsWindow, setMetricsWindow } = useAdmin();
  const [expandUserRank, setExpandUserRank] = useState(false);
  const read = useAdminRead(
    () =>
      expandUserRank
        ? api.getDashboard(metricsWindow, 'user_rank')
        : api.getDashboard(metricsWindow),
    [api, expandUserRank, metricsWindow],
  );
  /** 已点重试的卡 key：该卡先回骨架，本次重拉完成后清除。 */
  const [retryingKeys, setRetryingKeys] = useState<ReadonlySet<string>>(() => new Set());
  const copyDashboard = copy.admin.dashboard;

  // 本次重拉完成后清除「重试中」标记（成功 → 新数据；失败 → 回错误态）。
  useEffect(() => {
    if (!read.loading) {
      setRetryingKeys(new Set());
    }
  }, [read.loading]);

  /** 单卡重试：只重置该卡状态（回骨架）后整体重拉（数据单端点）。 */
  const retryCard = useCallback(
    (key: string) => {
      setRetryingKeys((current) => new Set(current).add(key));
      read.reload();
    },
    [read],
  );

  // 窗口切换刷新中：旧卡保留、数值区淡出（新数据落地后错峰淡入）。
  const switching = read.loading && read.data !== null;

  // 全局卡序号（跨包连续编号）：窗口切换错峰 delay = index × 30ms。
  let cardIndex = 0;

  return (
    <div className="flex flex-col gap-8 pr-4">
      <div className="flex items-center justify-between">
        <h2 className="text-[20px] font-medium text-ink-black">{copyDashboard.title}</h2>
        <SegmentedControl
          options={[...METRICS_WINDOW_OPTIONS]}
          value={metricsWindow}
          onChange={(value) => setMetricsWindow(value as MetricsWindow)}
          ariaLabel={copyDashboard.windowAria}
        />
      </div>
      {read.data === null && read.loading && <LoadingCards count={2} />}
      {read.data === null && read.error && (
        <ErrorState text={copyDashboard.loadError} onRetry={read.reload} />
      )}
      {read.data !== null &&
        (read.data.packs.length === 0 ? (
          <EmptyState text={copyDashboard.empty} />
        ) : (
          read.data.packs.map((pack) => (
            <section key={pack.key} className="flex flex-col">
              <h3 className="text-[20px] font-medium text-ink-black">{pack.title}</h3>
              {pack.description !== undefined && (
                <p className="mt-1 text-[15px] text-slate-gray">{pack.description}</p>
              )}
              <div className="mt-3 grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-4">
                {pack.cards.map((card) => {
                  const index = cardIndex;
                  cardIndex += 1;
                  return (
                    <DashboardCardView
                      key={card.key}
                      card={card}
                      index={index}
                      switching={switching}
                      error={read.error}
                      retrying={read.loading && retryingKeys.has(card.key)}
                      onRetry={() => retryCard(card.key)}
                      onExpand={card.kind === 'user_rank' ? () => setExpandUserRank(true) : undefined}
                    />
                  );
                })}
              </div>
            </section>
          ))
        ))}
    </div>
  );
}
