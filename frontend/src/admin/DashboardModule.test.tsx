/*
 * 总览 dashboard 测试（spec §2；验收 A13–A19）。
 * 经 fake AdminApi + MemoryRouter + AdminProvider 渲染：两种角色组包渲染、四卡型结构、
 * 超阈整卡变红（class + data 属性）、link 卡整卡可点导航（formatDrawerLocation 目标）、
 * 无数据卡「—」与「暂无数据」、user_rank 展开/收起、每包 description 有无、
 * 窗口切换新请求与错峰交叉淡变（旧 150ms 出 → 新 250ms 入，delay = index×30ms）、
 * sparkline 400ms 形变终态、首载/刷新失败与单卡重试骨架；内容区根右 padding（滚动条留白）。
 * 动画断言以终态与 transition/delay 属性为准（真实时钟，参照 DrawerHost.test.tsx 头注约定）。
 */

import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { fakeAdminApi } from '../test/auth-fixtures';
import { AdminProvider } from './AdminProvider';
import { formatSparkPoints, normalizeSparkPoints } from './dashboard/DashboardSparkline';
import { DashboardModule } from './DashboardModule';
import type {
  DashboardResponse,
  MetricsWindow,
  StatDashboardCard,
} from './types';

const copyDashboard = copy.admin.dashboard;

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location-path">{location.pathname}</output>;
}

function renderDashboard(api = fakeAdminApi()) {
  return render(
    <MemoryRouter initialEntries={['/admin/dashboard']}>
      <AdminProvider api={api}>
        <DashboardModule />
        <LocationProbe />
      </AdminProvider>
    </MemoryRouter>,
  );
}

interface Deferred<T> {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
  readonly reject: (reason?: unknown) => void;
}

function createDeferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/* ---------- 夹具：ops 组包（threshold/link 数据驱动）与 admin 组包（description + user_rank） ---------- */

function statCard(overrides: Partial<StatDashboardCard> = {}): StatDashboardCard {
  return {
    key: 'stat',
    title: '指标',
    kind: 'stat',
    value: 26,
    delta: { direction: 'up', text_hint: '+3' },
    sparkline: [4, 6, 5, 9, 12],
    threshold: null,
    link: null,
    ...overrides,
  };
}

function opsDashboard(window: MetricsWindow): DashboardResponse {
  const factor = window === '30d' ? 2 : 1;
  return {
    window,
    packs: [
      {
        key: 'tasks_health',
        title: '任务与健康',
        cards: [
          statCard({
            key: 'backlog',
            title: '入库队列积压',
            value: 26 * factor,
            sparkline: [4, 6, 5, 9, 12].map((value) => value * factor),
            threshold: { value: 20, direction: 'above' },
            link: 'ops.jobs',
          }),
          statCard({
            key: 'cache',
            title: '缓存命中率',
            value: 0.5,
            delta: { direction: 'down', text_hint: '-3.1%' },
            sparkline: [0.6, 0.58, 0.55, 0.52, 0.5],
            threshold: { value: 0.6, direction: 'below' },
            link: 'ops.metrics',
          }),
          {
            key: 'ocr',
            title: 'OCR 置信度分布',
            kind: 'distribution',
            rows: [
              { label: '90–100%', value: 82 * factor, ratio: 0.82, tone: 'normal' },
              { label: '<90%', value: 18 * factor, ratio: 0.18, tone: 'warning' },
            ],
            threshold: null,
            link: 'ops.metrics',
          },
          {
            key: 'quota',
            title: '配额申请待处理数',
            kind: 'count',
            value: 3,
            delta: null,
            threshold: null,
            link: 'ops.approvals.quota',
          },
          statCard({
            key: 'latency',
            title: 'API 延迟',
            value: null,
            delta: null,
            sparkline: [],
            threshold: { value: 800, direction: 'above' },
            link: 'ops.metrics',
          }),
          // 常态（未超阈）link 卡：结构 / 悬停态断言用它，避开超阈变红干扰
          statCard({
            key: 'llm',
            title: 'LLM 调用量',
            value: 1280 * factor,
            delta: { direction: 'flat', text_hint: '0' },
            sparkline: [900, 980, 1050, 1180, 1280],
            threshold: null,
            link: 'ops.metrics',
          }),
        ],
      },
    ],
  };
}

function adminDashboard(): DashboardResponse {
  const rankRows = Array.from({ length: 15 }, (_unused, index) => {
    const value = 900 - index * 50;
    return { label: `user_${index + 1}`, value, ratio: value / 900 };
  });
  return {
    window: '7d',
    packs: [
      {
        key: 'usage_overview',
        title: '使用概览',
        description: copyDashboard.packs.usageOverview,
        cards: [
          statCard({ key: 'active_users', title: '活跃用户数', value: 86, delta: null }),
        ],
      },
      {
        key: 'cost_share',
        title: '成本分摊',
        description: copyDashboard.packs.costShare,
        cards: [
          {
            key: 'rank',
            title: '按用户分摊',
            kind: 'user_rank',
            rows: rankRows,
            total_count: rankRows.length,
            threshold: null,
            link: null,
          },
        ],
      },
    ],
  };
}

function cardElement(container: HTMLElement, key: string): HTMLElement {
  const card = container.querySelector(`[data-card-key="${key}"]`);
  if (card === null) {
    throw new Error(`card ${key} not found`);
  }
  return card as HTMLElement;
}

function cardBody(card: HTMLElement): HTMLElement {
  const body = card.querySelector('[data-card-body]');
  if (body === null) {
    throw new Error('card body not found');
  }
  return body as HTMLElement;
}

describe('总览 dashboard：组包渲染（数据驱动，不判角色）', () => {
  it('内容区根右 padding：溢出滚动时右列卡片与纵向滚动条之间保留留白（A3；纯间距亮/暗主题均成立）', async () => {
    const { container } = renderDashboard();
    await screen.findByText(copyDashboard.title);
    expect(container.firstElementChild?.className).toContain('pr-4');
  });

  it('运维组包：包标题 + stat/distribution/count 卡；包无 description 说明行', async () => {
    const { container } = renderDashboard(
      fakeAdminApi({ getDashboard: vi.fn(async (window: MetricsWindow) => opsDashboard(window)) }),
    );
    expect(await screen.findByText('任务与健康')).toBeInTheDocument();
    expect(screen.getByText('入库队列积压')).toBeInTheDocument();
    expect(screen.getByText('OCR 置信度分布')).toBeInTheDocument();
    expect(screen.getByText('配额申请待处理数')).toBeInTheDocument();
    // 运维包不带 description：不渲染任何包说明行
    for (const text of Object.values(copyDashboard.packs)) {
      expect(screen.queryByText(text)).not.toBeInTheDocument();
    }
    expect(container.querySelectorAll('[data-card-key]').length).toBe(6);
  });

  it('超管组包：包标题下渲染 15px slate 说明行（pack.description 有则渲染）', async () => {
    renderDashboard(fakeAdminApi({ getDashboard: vi.fn(async () => adminDashboard()) }));
    expect(await screen.findByText('使用概览')).toBeInTheDocument();
    expect(screen.getByText(copyDashboard.packs.usageOverview)).toBeInTheDocument();
    expect(screen.getByText('成本分摊')).toBeInTheDocument();
    expect(screen.getByText(copyDashboard.packs.costShare)).toBeInTheDocument();
  });
});

describe('总览 dashboard：四卡型结构', () => {
  async function renderOps() {
    const rendered = renderDashboard(
      fakeAdminApi({ getDashboard: vi.fn(async (window: MetricsWindow) => opsDashboard(window)) }),
    );
    await screen.findByText('任务与健康');
    return rendered;
  }

  it('stat 卡：指标名 15px slate + 主数值 20px + delta 行（↑/↓/flat）+ sienna-brown sparkline', async () => {
    const { container } = await renderOps();
    // 常态卡（未超阈）：指标名 slate、主数值墨色
    const card = cardElement(container, 'llm');
    const title = within(card).getByText('LLM 调用量');
    expect(title.className).toContain('text-[15px]');
    expect(title.className).toContain('text-slate-gray');
    const value = within(card).getByText('1280');
    expect(value.className).toContain('text-[20px]');
    expect(value.className).toContain('font-medium');
    expect(value.className).toContain('text-ink-black');
    // flat direction：无箭头，text_hint 机读值原样出现
    expect(within(card).getByText('0')).toBeInTheDocument();
    const svg = card.querySelector('svg');
    expect(svg).not.toBeNull();
    expect(svg).toHaveAttribute('data-morph-ms', '400');
    const polyline = card.querySelector('polyline');
    expect(polyline).toHaveAttribute('stroke', 'var(--color-sienna-brown)');
    expect(polyline).toHaveAttribute('stroke-width', '1.5');
    expect(polyline?.getAttribute('points')).toBe(
      formatSparkPoints(normalizeSparkPoints([900, 980, 1050, 1180, 1280])),
    );
    // up → ↑ / down → ↓（delta 行不随超阈变色）
    expect(within(cardElement(container, 'backlog')).getByText('↑ +3')).toBeInTheDocument();
    expect(within(cardElement(container, 'cache')).getByText('↓ -3.1%')).toBeInTheDocument();
  });

  it('distribution 卡：标签 + ratio 条形（400ms 宽度过渡）+ 数值；warning 行文字琥珀', async () => {
    const { container } = await renderOps();
    const card = cardElement(container, 'ocr');
    expect(within(card).getByText('90–100%')).toBeInTheDocument();
    expect(within(card).getByText('82')).toBeInTheDocument();
    const bars = card.querySelectorAll('.bg-sienna-brown');
    expect(bars.length).toBe(2);
    expect((bars[0] as HTMLElement).style.width).toBe('82%');
    expect((bars[0] as HTMLElement).style.transition).toContain('width 400ms');
    expect((bars[1] as HTMLElement).style.width).toBe('18%');
    // tone=warning 行文字转琥珀（text-warning）
    expect(within(card).getByText('<90%').className).toContain('text-warning');
    expect(within(card).getByText('18').className).toContain('text-warning');
  });

  it('count 卡：无 sparkline；待处理数 >0 时数值旁 CountBadge', async () => {
    const { container } = await renderOps();
    const card = cardElement(container, 'quota');
    expect(card.querySelector('svg')).toBeNull();
    const badge = card.querySelector('.bg-mist-gray');
    expect(badge).not.toBeNull();
    expect(badge?.textContent).toBe('3');
  });

  it('user_rank 卡：前 10 行 +「展开全部」至上限行数，再点「收起」', async () => {
    const { container } = renderDashboard(
      fakeAdminApi({ getDashboard: vi.fn(async () => adminDashboard()) }),
    );
    await screen.findByText('成本分摊');
    const card = cardElement(container, 'rank');
    expect(card.querySelectorAll('ul li').length).toBe(10);
    // 行结构：名次 Sohne 500 16px ink + 名称 + 数值 15px slate（无奖牌彩色）
    const firstRow = card.querySelector('ul li') as HTMLElement;
    expect(within(firstRow).getByText('1').className).toContain('text-[16px]');
    expect(within(firstRow).getByText('1').className).toContain('font-medium');
    expect(within(firstRow).getByText('user_1')).toBeInTheDocument();
    expect(within(firstRow).getByText('900').className).toContain('text-slate-gray');

    const user = userEvent.setup();
    await user.click(within(card).getByText(copyDashboard.expandAll));
    expect(card.querySelectorAll('ul li').length).toBe(15);
    await user.click(within(card).getByText(copyDashboard.collapse));
    expect(card.querySelectorAll('ul li').length).toBe(10);
  });
});

describe('总览 dashboard：超阈与整卡跳转', () => {
  it('超阈整卡变红：danger 10% 铺底 + 1px danger 环 + 数值与指标名转 danger（above 与 below 两方向）', async () => {
    const { container } = renderDashboard(
      fakeAdminApi({ getDashboard: vi.fn(async (window: MetricsWindow) => opsDashboard(window)) }),
    );
    await screen.findByText('任务与健康');
    // above：26 > 20 超阈
    const backlog = cardElement(container, 'backlog');
    expect(backlog).toHaveAttribute('data-breached', 'true');
    expect(backlog.className).toContain('bg-danger/10');
    expect(backlog.style.boxShadow).toContain('var(--color-danger)');
    expect(within(backlog).getByText('入库队列积压').className).toContain('text-danger');
    expect(within(backlog).getByText('26').className).toContain('text-danger');
    // below：0.5 < 0.6 超阈（命中率类）
    const cache = cardElement(container, 'cache');
    expect(cache).toHaveAttribute('data-breached', 'true');
    expect(cache.className).toContain('bg-danger/10');
    // 未超阈卡：无铺底、无 data 属性
    const quota = cardElement(container, 'quota');
    expect(quota).not.toHaveAttribute('data-breached');
    expect(quota.className).not.toContain('bg-danger/10');
  });

  it('link 非空整卡可点：role=button + hover 态，点击经 formatDrawerLocation 导航；link 为 null 不可点', async () => {
    const { container } = renderDashboard(
      fakeAdminApi({ getDashboard: vi.fn(async (window: MetricsWindow) => opsDashboard(window)) }),
    );
    await screen.findByText('任务与健康');
    // 常态 link 卡：role=button + cursor pointer + hover fog-white
    const llm = cardElement(container, 'llm');
    expect(llm).toHaveAttribute('data-link', 'ops.metrics');
    expect(llm).toHaveAttribute('role', 'button');
    expect(llm.className).toContain('cursor-pointer');
    expect(llm.className).toContain('hover:bg-fog-white');
    // 超阈 link 卡仍可点（cursor 保留；铺底让位于 danger 10%）
    const backlog = cardElement(container, 'backlog');
    expect(backlog).toHaveAttribute('data-link', 'ops.jobs');
    expect(backlog).toHaveAttribute('role', 'button');
    expect(backlog.className).toContain('cursor-pointer');

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /入库队列积压/ }));
    expect(screen.getByTestId('location-path').textContent).toBe('/admin/operations/jobs');
  });

  it('link 为 null 的卡不可点：无 role=button、无 hover 态', async () => {
    const { container } = renderDashboard(
      fakeAdminApi({ getDashboard: vi.fn(async () => adminDashboard()) }),
    );
    await screen.findByText('使用概览');
    const card = cardElement(container, 'active_users');
    expect(card).not.toHaveAttribute('role');
    expect(card.className).not.toContain('cursor-pointer');
    expect(card.className).not.toContain('hover:bg-fog-white');
  });

  it('无数据卡：value null → 数字位「—」+ 图形区「暂无数据」smoke-gray（不参与超阈判定）', async () => {
    const { container } = renderDashboard(
      fakeAdminApi({ getDashboard: vi.fn(async (window: MetricsWindow) => opsDashboard(window)) }),
    );
    await screen.findByText('任务与健康');
    const card = cardElement(container, 'latency');
    expect(within(card).getByText('—')).toBeInTheDocument();
    expect(within(card).getByText(copyDashboard.noData).className).toContain('text-smoke-gray');
    expect(card.querySelector('svg polyline')?.getAttribute('points') ?? '').toBe('');
    expect(card).not.toHaveAttribute('data-breached');
  });
});

describe('总览 dashboard：窗口切换动效（A18）', () => {
  it('切换触发新请求；旧值 150ms 淡出 → 新值 250ms 错峰淡入；sparkline 400ms 形变至终态', async () => {
    const d30 = createDeferred<DashboardResponse>();
    const getDashboard = vi.fn((window: MetricsWindow) =>
      window === '7d' ? Promise.resolve(opsDashboard('7d')) : d30.promise,
    );
    const { container } = renderDashboard(fakeAdminApi({ getDashboard }));
    await screen.findByText('任务与健康');
    const backlog = cardElement(container, 'backlog');
    const cache = cardElement(container, 'cache');
    const ocr = cardElement(container, 'ocr');

    const user = userEvent.setup();
    await user.click(screen.getByRole('radio', { name: copyDashboard.d30 }));
    expect(getDashboard).toHaveBeenCalledWith('30d');

    // 切换中：旧值保留且 150ms 淡出（opacity 0、--duration-fast、无错峰 delay）
    expect(within(backlog).getByText('26')).toBeInTheDocument();
    expect(cardBody(backlog).style.opacity).toBe('0');
    expect(cardBody(backlog).style.transitionDuration).toBe('var(--duration-fast)');
    expect(cardBody(backlog).style.transitionDelay).toBe('0ms');

    await act(async () => {
      d30.resolve(opsDashboard('30d'));
    });
    // 新值落地：250ms（--duration-base）淡入，错峰 delay = index × 30ms
    expect(await within(backlog).findByText('52')).toBeInTheDocument();
    expect(cardBody(backlog).style.opacity).toBe('1');
    expect(cardBody(backlog).style.transitionDuration).toBe('var(--duration-base)');
    expect(cardBody(backlog).style.transitionDelay).toBe('0ms');
    expect(cardBody(cache).style.transitionDelay).toBe('30ms');
    expect(cardBody(ocr).style.transitionDelay).toBe('60ms');
    expect(cardBody(backlog)).toHaveAttribute('data-stagger-index', '0');
    expect(cardBody(cache)).toHaveAttribute('data-stagger-index', '1');
    expect(cardBody(ocr)).toHaveAttribute('data-stagger-index', '2');

    // sparkline 400ms 形变：终态精确落到新序列归一化点
    const polyline = backlog.querySelector('polyline');
    await waitFor(
      () =>
        expect(polyline?.getAttribute('points')).toBe(
          formatSparkPoints(normalizeSparkPoints([8, 12, 10, 18, 24])),
        ),
      { timeout: 2000 },
    );
    // 超阈状态随新数据重算：52 > 20 仍超阈
    expect(backlog).toHaveAttribute('data-breached', 'true');
  });
});

describe('总览 dashboard：卡六态之加载 / 错误', () => {
  it('首载失败整区错误态；重试后整区恢复', async () => {
    const getDashboard = vi
      .fn()
      .mockRejectedValueOnce(new Error('boom'))
      .mockImplementation(async (window: MetricsWindow) => opsDashboard(window));
    renderDashboard(fakeAdminApi({ getDashboard }));
    expect(await screen.findByText(copyDashboard.loadError)).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByText(copy.states.retry));
    expect(await screen.findByText('任务与健康')).toBeInTheDocument();
    expect(getDashboard).toHaveBeenCalledTimes(2);
  });

  it('刷新失败旧卡保留并逐卡错误态；点重试只重置该卡（回骨架）后整体重拉', async () => {
    const d30 = createDeferred<DashboardResponse>();
    const retry = createDeferred<DashboardResponse>();
    const getDashboard = vi
      .fn()
      .mockImplementationOnce(() => Promise.resolve(opsDashboard('7d')))
      .mockImplementationOnce(() => d30.promise)
      .mockImplementationOnce(() => retry.promise);
    const { container } = renderDashboard(fakeAdminApi({ getDashboard }));
    await screen.findByText('任务与健康');
    const backlog = cardElement(container, 'backlog');
    const cache = cardElement(container, 'cache');

    const user = userEvent.setup();
    await user.click(screen.getByRole('radio', { name: copyDashboard.d30 }));
    await act(async () => {
      d30.reject(new Error('boom'));
    });
    // 刷新失败：旧卡保留在网格位，卡内居中 15px danger 说明 + 重试文字链
    await waitFor(() =>
      expect(backlog.querySelector('[data-card-state="error"]')).not.toBeNull(),
    );
    for (const key of ['backlog', 'cache', 'ocr', 'quota', 'latency']) {
      expect(cardElement(container, key).querySelector('[data-card-state="error"]')).not.toBeNull();
    }
    expect(within(backlog).getByText(copyDashboard.loadError).className).toContain('text-danger');
    // 错误态整卡不可点
    expect(backlog).not.toHaveAttribute('role');

    // 点该卡重试：只重置该卡（回骨架），随后整体重拉
    await user.click(within(backlog).getByText(copy.states.retry));
    expect(getDashboard).toHaveBeenCalledTimes(3);
    expect(backlog.querySelector('[data-card-state="loading"]')).not.toBeNull();
    expect(backlog.querySelectorAll('.ui-skeleton').length).toBeGreaterThan(0);
    // 其余卡退出错误态、旧数据淡出等待新数据
    expect(cache.querySelector('[data-card-state="error"]')).toBeNull();
    expect(cardBody(cache).style.opacity).toBe('0');

    await act(async () => {
      retry.resolve(opsDashboard('30d'));
    });
    expect(await within(backlog).findByText('52')).toBeInTheDocument();
    expect(backlog.querySelector('[data-card-state="loading"]')).toBeNull();
    expect(backlog.querySelector('[data-card-state="error"]')).toBeNull();
  });
});
