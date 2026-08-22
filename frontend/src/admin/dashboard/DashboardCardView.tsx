/*
 * dashboard 指标卡视图（spec §2；运维端 §7.2 / 超管端 §7.2）。
 * 四卡型按 card.kind 判别渲染（stat / distribution / count / user_rank），前端不做角色分支：
 * - stat：指标名 15px slate + 主数值 Sohne 500 20px ink + delta 行 15px slate
 *   （up→↑ / down→↓ / flat 无箭头，text_hint 机读值原样出现）+ 无轴 sparkline；
 * - distribution：标签 15px ink + 条形（sienna-brown 高 8 圆角 4，宽=ratio×100%，
 *   数据变化宽度过渡 400ms --ease-in-out）+ 数值 15px slate；tone=warning 行文字转琥珀；
 * - count：无 sparkline；待处理数 >0 时数值旁 CountBadge（0 由 CountBadge 自行隐藏）；
 * - user_rank：前 10 行（名次 Sohne 500 16px ink + 名称 + 数值 15px slate，无奖牌彩色），
 *   行尾「展开全部」TextLink 展开至 50 行，再点「收起」。
 * 超阈（threshold 仅数据驱动，direction below 用于命中率类）：整卡 danger 10% 铺底 +
 * 1px danger 环 + 主数值与指标名转 danger，background-color 250ms 过渡；恢复同样 250ms。
 * 整卡可点：card.link 非空 → role=button + hover fog-white 150ms + cursor pointer，
 * 点击经 LINK_TARGETS formatDrawerLocation navigate；link 为 null 不可点（无 hover 态）。
 * 卡六态：默认 / 悬停 / 超阈 / 加载（卡内骨架呼吸：标题条 + 数字条 + 波形占位）/
 * 错误（卡内居中 15px danger 说明 +「重试」TextLink，只重置该卡状态后整体重拉）/
 * 无数据（value null → 数字位「—」+ 图形区「暂无数据」smoke-gray）。
 * 窗口切换：数值区交叉淡变（旧 150ms 淡出 → 新 250ms 淡入，错峰 delay = index × 30ms），
 * sparkline 400ms 形变；prefers-reduced-motion 全部直出。
 * DashboardCardView 同时供系统运维指标看板（§9.2 固定三卡）复用。
 */

import { useCallback, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from 'react';
import { useNavigate } from 'react-router';
import { copy } from '../../copy';
import { formatDrawerLocation, type DrawerSegment } from '../../router/drawer-params';
import { CountBadge, TextLink } from '../../ui';
import type {
  DashboardCard,
  DashboardLinkKey,
  DistributionDashboardCard,
  MetricDelta,
  MetricDeltaDirection,
  UserRankDashboardCard,
} from '../types';
import { DashboardSparkline } from './DashboardSparkline';
import { useReducedMotion } from './use-reduced-motion';

/** 待办卡与指标卡的整卡跳转目标（§7.2 下钻路由）。 */
export const LINK_TARGETS: Record<DashboardLinkKey, { segment: DrawerSegment; drill: readonly string[] }> = {
  'ops.jobs': { segment: 'admin', drill: ['operations', 'jobs'] },
  'ops.metrics': { segment: 'admin', drill: ['operations', 'metrics'] },
  'ops.approvals.quota': { segment: 'admin', drill: ['approvals', 'quota'] },
  'ops.approvals.submissions': { segment: 'admin', drill: ['approvals', 'submissions'] },
  'ops.spaces.public': { segment: 'admin', drill: ['spaces', 'public'] },
};

/** 阈值超限判定：above 高于阈值为异常（积压类）；below 低于下限为异常（命中率类）。 */
export function isBreached(card: DashboardCard): boolean {
  if (card.threshold === null || card.kind === 'distribution' || card.kind === 'user_rank') {
    return false;
  }
  if (card.value === null) {
    return false;
  }
  return card.threshold.direction === 'above'
    ? card.value > card.threshold.value
    : card.value < card.threshold.value;
}

/** user_rank 排行卡：收起前 10 行，展开至响应上限 50 行。 */
const USER_RANK_COLLAPSED = 10;
const USER_RANK_LIMIT = 50;

/** 窗口切换交叉淡变：旧值 150ms（--duration-fast）淡出 → 新值 250ms（--duration-base）淡入，
 *  第 i 张卡 delay i × 30ms。 */
const STAGGER_MS = 30;

const DELTA_ARROWS: Record<MetricDeltaDirection, string | null> = {
  up: '↑',
  down: '↓',
  flat: null,
};

function clampRatio(ratio: number): number {
  return Math.min(1, Math.max(0, ratio));
}

/** 分布 / 排行条形：sienna-brown 高 8 圆角 4，宽 = ratio × 100%，数据变化 400ms 过渡。 */
function RatioBar({ ratio }: { readonly ratio: number }) {
  return (
    <span className="flex-1">
      <span
        className="block h-2 rounded-[4px] bg-sienna-brown"
        style={{
          width: `${clampRatio(ratio) * 100}%`,
          transition: 'width 400ms var(--ease-in-out)',
        }}
      />
    </span>
  );
}

function DeltaRow({ delta }: { readonly delta: MetricDelta }) {
  const arrow = DELTA_ARROWS[delta.direction];
  return (
    <p className="mt-2 text-[15px] text-slate-gray">
      {arrow === null ? delta.text_hint : `${arrow} ${delta.text_hint}`}
    </p>
  );
}

function DistributionRows({ card }: { readonly card: DistributionDashboardCard }) {
  return (
    <ul className="mt-3 flex flex-col gap-2">
      {card.rows.map((row) => {
        const warning = row.tone === 'warning';
        return (
          <li key={row.label} className="flex items-center gap-3">
            <span
              className={`w-20 shrink-0 truncate text-[15px] ${warning ? 'text-warning' : 'text-ink-black'}`}
            >
              {row.label}
            </span>
            <RatioBar ratio={row.ratio} />
            <span className={`shrink-0 text-[15px] ${warning ? 'text-warning' : 'text-slate-gray'}`}>
              {row.value}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function UserRankRows({
  card,
  onExpand,
}: {
  readonly card: UserRankDashboardCard;
  readonly onExpand?: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const rows = expanded
    ? card.rows.slice(0, USER_RANK_LIMIT)
    : card.rows.slice(0, USER_RANK_COLLAPSED);
  return (
    <>
      <ul className="mt-3 flex flex-col gap-2">
        {rows.map((row, rowIndex) => (
          <li key={row.label} className="flex items-center gap-3">
            <span className="w-6 shrink-0 text-[16px] font-medium text-ink-black">
              {rowIndex + 1}
            </span>
            <span className="w-20 shrink-0 truncate text-[15px] text-ink-black">{row.label}</span>
            <RatioBar ratio={row.ratio} />
            <span className="shrink-0 text-[15px] text-slate-gray">{row.value}</span>
          </li>
        ))}
      </ul>
      {card.total_count > USER_RANK_COLLAPSED && (
        <TextLink
          className="mt-2"
          onClick={() => {
            if (!expanded && onExpand !== undefined && card.rows.length < card.total_count) {
              onExpand();
            }
            setExpanded((current) => !current);
          }}
        >
          {expanded ? copy.admin.dashboard.collapse : copy.admin.dashboard.expandAll}
        </TextLink>
      )}
    </>
  );
}

export interface DashboardCardViewProps {
  readonly card: DashboardCard;
  /** 全局卡序号：窗口切换错峰 delay = index × 30ms。 */
  readonly index?: number;
  /** 窗口切换刷新中（旧数据 150ms 淡出；新数据落地 250ms 错峰淡入）。 */
  readonly switching?: boolean;
  /** 本次刷新失败：卡内错误态（保留卡在网格位，仅该卡重试）。 */
  readonly error?: boolean;
  /** 本卡已点重试：卡内骨架呼吸直到本次重拉完成。 */
  readonly retrying?: boolean;
  readonly onRetry?: () => void;
  readonly onExpand?: () => void;
}

export function DashboardCardView({
  card,
  index = 0,
  switching = false,
  error = false,
  retrying = false,
  onRetry,
  onExpand,
}: DashboardCardViewProps) {
  const navigate = useNavigate();
  const reducedMotion = useReducedMotion();
  const copyDashboard = copy.admin.dashboard;
  const breached = !error && !retrying && isBreached(card);
  // 整卡可点仅 link 非空且非错误/重试态；user_rank 含展开交互，不外包可点容器。
  const clickable = card.link !== null && card.kind !== 'user_rank' && !error && !retrying;

  const handleNavigate = useCallback(() => {
    if (card.link === null) {
      return;
    }
    const target = LINK_TARGETS[card.link];
    navigate(formatDrawerLocation({ open: true, segment: target.segment, drill: target.drill }));
  }, [card.link, navigate]);

  const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      handleNavigate();
    }
  };

  let body: ReactNode;
  if (retrying) {
    // 加载态：卡内骨架呼吸（标题条 + 数字条 + 波形占位）。
    body = (
      <div aria-busy="true" data-card-state="loading">
        <div className="ui-skeleton h-4 w-1/3 rounded-[var(--radius-images)] bg-mist-gray" />
        <div className="ui-skeleton mt-2 h-5 w-1/2 rounded-[var(--radius-images)] bg-mist-gray" />
        <div className="ui-skeleton mt-4 h-8 w-full rounded-[var(--radius-images)] bg-mist-gray" />
      </div>
    );
  } else if (error) {
    // 错误态：卡内居中 15px danger 说明 + 重试文字链（只重置该卡，重试即整体重拉）。
    body = (
      <div className="flex flex-col items-center gap-2 py-4" data-card-state="error">
        <p className="text-[15px] text-danger">{copyDashboard.loadError}</p>
        {onRetry !== undefined && <TextLink onClick={onRetry}>{copy.states.retry}</TextLink>}
      </div>
    );
  } else {
    // 数值区交叉淡变：switching（旧值）150ms 淡出；新值 250ms 淡入，错峰 index × 30ms。
    const fadeStyle: CSSProperties | undefined = reducedMotion
      ? undefined
      : {
          transitionProperty: 'opacity',
          transitionDuration: `var(${switching ? '--duration-fast' : '--duration-base'})`,
          transitionTimingFunction: 'var(--ease-in-out)',
          transitionDelay: switching ? '0ms' : `${index * STAGGER_MS}ms`,
          opacity: switching ? 0 : 1,
        };
    let content: ReactNode;
    switch (card.kind) {
      case 'stat':
        content = (
          <>
            <div
              className={`mt-2 text-[20px] font-medium leading-none ${
                breached ? 'text-danger' : 'text-ink-black'
              }`}
            >
              {card.value ?? '—'}
            </div>
            {card.delta !== null && <DeltaRow delta={card.delta} />}
            {card.value === null ? (
              <div className="mt-3 flex h-8 items-center justify-center text-[15px] text-smoke-gray">
                {copyDashboard.noData}
              </div>
            ) : (
              <DashboardSparkline data={card.sparkline} animate={!reducedMotion} />
            )}
          </>
        );
        break;
      case 'count':
        content = (
          <>
            <div
              className={`mt-2 text-[20px] font-medium leading-none ${
                breached ? 'text-danger' : 'text-ink-black'
              }`}
            >
              {card.value === null ? (
                '—'
              ) : (
                <span className="inline-flex items-center gap-2">
                  {card.value}
                  <CountBadge count={card.value} />
                </span>
              )}
            </div>
            {card.delta !== null && <DeltaRow delta={card.delta} />}
            {card.value === null && (
              <p className="mt-2 text-[15px] text-smoke-gray">{copyDashboard.noData}</p>
            )}
          </>
        );
        break;
      case 'distribution':
        content = <DistributionRows card={card} />;
        break;
      case 'user_rank':
        content = <UserRankRows card={card} onExpand={onExpand} />;
        break;
    }
    body = (
      <div data-card-body data-stagger-index={index} style={fadeStyle}>
        {content}
      </div>
    );
  }

  return (
    <section
      data-card-key={card.key}
      data-breached={breached || undefined}
      data-link={card.link ?? undefined}
      role={clickable ? 'button' : undefined}
      tabIndex={clickable ? 0 : undefined}
      onClick={clickable ? handleNavigate : undefined}
      onKeyDown={clickable ? handleKeyDown : undefined}
      className={
        'rounded-[var(--radius-elevatedcards)] p-5 shadow-[var(--shadow-subtle)] ' +
        // 悬停（link 卡）：卡底 fog-white 150ms；超阈：danger 10% 铺底 250ms 过渡（悬停态让位）。
        `transition-[background-color,box-shadow] ease-[var(--ease-in-out)] ${
          breached
            ? 'bg-danger/10 duration-[var(--duration-base)]'
            : clickable
              ? 'bg-paper-white transition-colors duration-[var(--duration-fast)] hover:bg-fog-white'
              : 'bg-paper-white duration-[var(--duration-base)]'
        } ${clickable ? 'cursor-pointer' : ''}`
      }
      // 超阈：1px danger 环（叠于 shadow-subtle 发丝环之上）+ 环色 250ms 过渡。
      style={
        breached
          ? { boxShadow: '0 0 0 1px var(--color-danger), var(--shadow-subtle)' }
          : undefined
      }
    >
      <h3 className={`text-[15px] ${breached ? 'text-danger' : 'text-slate-gray'}`}>{card.title}</h3>
      {body}
    </section>
  );
}
