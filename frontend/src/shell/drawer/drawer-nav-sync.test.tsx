/*
 * 抽屉左栏选中态一致性回归（fix-drawer-nav-highlight-race；shared-shell 规格 §1）。
 * 根因：react-router 默认把 location 更新包进 startTransition，快速连切时被模块挂载
 * 数据加载等 urgent 更新反复抢占，URL（pushState 同步）与抽屉 UI 错位百毫秒级；
 * 修复为 App 装配 BrowserRouter useTransitions={false}（同步提交，消除滞后窗口）。
 * jsdom 下 act 会冲刷 transition，滞后台阶仅真实浏览器可复现——竞态时序守卫在
 * e2e/drawer-nav-sync.spec.ts；本文件锁逻辑不变量：快速连切 / 下钻打断 / 铃铛深链 /
 * 深链恢复全部路径下，左栏选中 == 右栏当前层 == URL（规格 §1 一致性条款）。
 */

import { fireEvent, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useLocation } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import type { AdminApi } from '../../admin/api';
import { copy } from '../../copy';
import type { NotificationItem } from '../../notifications/types';
import { NotificationsStore } from '../../notifications/store';
import { AppRoutes } from '../../router/AppRoutes';
import {
  createAuthedStore,
  fakeAdminApi,
  fakeNotificationsApi,
  renderWithShell,
  testUser,
} from '../../test/auth-fixtures';

const drawerCopy = copy.shell.drawer;
const modules = drawerCopy.modules;

type TestRole = 'user' | 'minister' | 'ops' | 'admin';

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location-path">{location.pathname}</output>;
}

async function renderApp(
  path: string,
  role: TestRole = 'user',
  options: { adminApi?: AdminApi; notifications?: NotificationsStore } = {},
) {
  const store = await createAuthedStore(testUser({ role }));
  renderWithShell(
    <>
      <AppRoutes />
      <LocationProbe />
    </>,
    store,
    [path],
    { adminApi: options.adminApi, notifications: options.notifications },
  );
  return screen.getByTestId('location-path');
}

/** 左栏 nav 内当前选中按钮文本（font-w480 为选中态标记；文本可能带计数徽标后缀）。 */
function selectedNavLabels(dialog: HTMLElement): string[] {
  const nav = dialog.querySelector('nav');
  if (nav === null) {
    return [];
  }
  return Array.from(nav.querySelectorAll('button'))
    .filter((button) => button.className.includes('font-w480'))
    .map((button) => button.textContent?.trim() ?? '');
}

function navOf(dialog: HTMLElement): HTMLElement {
  const nav = dialog.querySelector('nav');
  if (nav === null) {
    throw new Error('桌面端抽屉左栏 nav 缺失');
  }
  return nav as HTMLElement;
}

/** 深度 1 模块层一致性：URL == 目标路径，左栏唯一选中 == 目标模块，页头 h1 == 模块名（管理段）。 */
function expectModuleConsistent(
  dialog: HTMLElement,
  probe: HTMLElement,
  path: string,
  title: string,
) {
  expect(probe.textContent).toBe(path);
  const selected = selectedNavLabels(dialog);
  expect(selected.length).toBe(1);
  expect(selected[0]).toContain(title);
  expect(within(dialog).getByRole('heading', { level: 1 }).textContent).toBe(title);
}

/** 深度 ≥2 下钻层一致性：URL == 目标路径，左栏层名槽 == 当前层名，返回键指向上一层。 */
function expectDrilledConsistent(
  dialog: HTMLElement,
  probe: HTMLElement,
  path: string,
  layerTitle: string,
  parentTitle: string,
) {
  expect(probe.textContent).toBe(path);
  const slot = dialog.querySelector('[data-drill-title-slot]');
  expect(slot?.textContent).toBe(layerTitle);
  expect(
    within(dialog).getByRole('button', { name: drawerCopy.backAria(parentTitle) }),
  ).toBeInTheDocument();
}

describe('左栏选中态一致性：管理段快速连续切换（A2）', () => {
  it('六模块连续切换（总览数据挂起、内容尚在加载时打断）：每步 高亮==页头==URL', async () => {
    // 总览数据永不返回：模拟「内容尚在加载」期间被连续打断
    const adminApi = fakeAdminApi({
      getDashboard: vi.fn((): Promise<never> => new Promise(() => {})),
    });
    const probe = await renderApp('/admin/dashboard', 'ops', { adminApi });
    const dialog = await screen.findByRole('dialog', { name: modules.dashboard });
    const sequence = [
      { id: 'approvals', title: modules.approvals },
      { id: 'spaces', title: modules.spaces },
      { id: 'evaluation', title: modules.evaluation },
      { id: 'operations', title: modules.operations },
      { id: 'users', title: modules.usersOps },
      { id: 'dashboard', title: modules.dashboard },
    ];
    for (const step of sequence) {
      fireEvent.click(
        within(navOf(dialog)).getByRole('button', { name: new RegExp(`^${step.title}`) }),
      );
      expectModuleConsistent(dialog, probe, `/admin/${step.id}`, step.title);
    }
    // 内容区确认为目标模块：审批中心下钻行 / 知识空间下钻行 / 总览模块标题
    expect(
      within(dialog).getByRole('heading', { level: 2, name: copy.admin.dashboard.title }),
    ).toBeInTheDocument();
  });

  it('下钻动画进行中打断切换到其他模块：最终态与残留计时器走完后均一致', async () => {
    const probe = await renderApp('/admin/approvals', 'ops');
    const dialog = await screen.findByRole('dialog', { name: modules.approvals });
    // 下钻进配额申请（五步动画真实计时器 550ms 启动）
    fireEvent.click(
      within(dialog).getByRole('button', { name: new RegExp(`^${modules.quotaRequests}`) }),
    );
    expectDrilledConsistent(dialog, probe, '/admin/approvals/quota', modules.quotaRequests, modules.approvals);
    // 动画进行中立即打断：先回模块层再切知识空间
    fireEvent.click(
      within(dialog).getByRole('button', { name: drawerCopy.backAria(modules.approvals) }),
    );
    fireEvent.click(
      within(navOf(dialog)).getByRole('button', { name: new RegExp(`^${modules.spaces}`) }),
    );
    expectModuleConsistent(dialog, probe, '/admin/spaces', modules.spaces);
    expect(
      within(dialog).getByRole('button', { name: new RegExp(`^${modules.publicSpace}`) }),
    ).toBeInTheDocument();
    // 残留动画计时器（550ms）走完后仍一致
    await new Promise((resolve) => setTimeout(resolve, 600));
    expectModuleConsistent(dialog, probe, '/admin/spaces', modules.spaces);
  });

  it('下钻层进出（知识空间→公共库→返回）：左栏层名与内容逐层一致', async () => {
    const probe = await renderApp('/admin/spaces', 'ops');
    const dialog = await screen.findByRole('dialog', { name: modules.spaces });
    fireEvent.click(
      within(dialog).getByRole('button', { name: new RegExp(`^${modules.publicSpace}`) }),
    );
    expectDrilledConsistent(dialog, probe, '/admin/spaces/public', modules.publicSpace, modules.spaces);
    expect(
      await within(dialog).findByRole('heading', { level: 2, name: modules.publicSpace }),
    ).toBeInTheDocument();
    fireEvent.click(
      within(dialog).getByRole('button', { name: drawerCopy.backAria(modules.spaces) }),
    );
    expectModuleConsistent(dialog, probe, '/admin/spaces', modules.spaces);
  });
});

describe('左栏选中态一致性：个人段（A3）', () => {
  it('四模块与知识库下钻层连续切换一致', async () => {
    const probe = await renderApp('/settings');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    const sequence = [
      { id: 'profile', title: modules.profile },
      { id: 'security', title: modules.security },
      { id: 'appearance', title: modules.appearance },
      { id: 'knowledge', title: modules.knowledge },
    ];
    for (const step of sequence) {
      fireEvent.click(
        within(navOf(dialog)).getByRole('button', { name: new RegExp(`^${step.title}`) }),
      );
      expect(probe.textContent).toBe(`/settings/${step.id}`);
      const selected = selectedNavLabels(dialog);
      expect(selected.length).toBe(1);
      expect(selected[0]).toContain(step.title);
    }
    // 知识库内容在场（上传结果入口）
    expect(
      await within(dialog).findByText(copy.settings.knowledge.uploads.historyEntry),
    ).toBeInTheDocument();
    // 下钻上传结果层 → 返回 → 下钻我的投稿层
    fireEvent.click(
      within(dialog).getByRole('button', { name: copy.settings.knowledge.uploads.historyEntry }),
    );
    expectDrilledConsistent(
      dialog,
      probe,
      '/settings/knowledge/uploads',
      modules.uploads,
      modules.knowledge,
    );
    fireEvent.click(
      within(dialog).getByRole('button', { name: drawerCopy.backAria(modules.knowledge) }),
    );
    await within(dialog).findByText(copy.settings.knowledge.uploads.historyEntry);
    fireEvent.click(
      within(dialog).getByRole('button', { name: copy.settings.knowledge.submissions.entry }),
    );
    expectDrilledConsistent(
      dialog,
      probe,
      '/settings/knowledge/submissions',
      modules.submissions,
      modules.knowledge,
    );
    expect(
      await within(dialog).findByText(copy.settings.knowledge.submissions.title),
    ).toBeInTheDocument();
  });
});

describe('左栏选中态一致性：铃铛深链与深链恢复（A4）', () => {
  it('铃铛条目跳转到 /admin/spaces/public：左栏层名与恢复出的层一致', async () => {
    const item: NotificationItem = {
      id: 'n_1',
      type: 'graph_build_completed',
      title: '图谱构建完成',
      payload: { graph_build_id: 'gb_1', status: 'succeeded' },
      read: false,
      event_occurred_at: '2026-08-18T03:00:00Z',
    };
    const notifications = new NotificationsStore(
      fakeNotificationsApi({
        list: vi.fn(async () => ({ items: [item] })),
        unreadCount: vi.fn(async () => ({ count: 1 })),
      }),
    );
    const probe = await renderApp('/', 'ops', { notifications });
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: copy.notifications.bellAria }));
    await user.click(await screen.findByRole('button', { name: /图谱构建完成/ }));
    const dialog = await screen.findByRole('dialog', { name: modules.spaces });
    expectDrilledConsistent(dialog, probe, '/admin/spaces/public', modules.publicSpace, modules.spaces);
    expect(
      await within(dialog).findByRole('heading', { level: 2, name: modules.publicSpace }),
    ).toBeInTheDocument();
  });

  it('粘贴深链 /admin/operations/metrics 恢复：左栏层名与内容一致', async () => {
    const probe = await renderApp('/admin/operations/metrics', 'ops');
    const dialog = await screen.findByRole('dialog', { name: modules.operations });
    expectDrilledConsistent(
      dialog,
      probe,
      '/admin/operations/metrics',
      modules.opsMetrics,
      modules.operations,
    );
  });

  it('刷新式恢复 /settings/knowledge/uploads：左栏层名与内容一致', async () => {
    const probe = await renderApp('/settings/knowledge/uploads');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    expectDrilledConsistent(
      dialog,
      probe,
      '/settings/knowledge/uploads',
      modules.uploads,
      modules.knowledge,
    );
  });
});
