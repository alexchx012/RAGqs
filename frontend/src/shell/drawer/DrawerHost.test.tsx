/*
 * 全屏抽屉宿主集成测试（fe-shared-shell 规格 §1–§3、§7；共用基座 §5.1–§5.2）。
 * 经 AppRoutes 整树渲染（真实 URL 驱动）：开合、深链恢复、未注册层占位、按角色左栏、
 * 跨段切换、下钻与返回、Esc 逐层、关闭按钮、下滑手势、管理段顶层自动选中总览、窄屏单栏化、
 * prefers-reduced-motion 降级。
 * 动画计时器走真实时钟（进入 400ms / 关闭 400ms / 下钻 550ms），断言最终稳定状态。
 */

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useLocation } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import type { AdminApi } from '../../admin/api';
import { copy } from '../../copy';
import { AppRoutes } from '../../router/AppRoutes';
import { createAuthedStore, fakeAdminApi, renderWithShell, testUser } from '../../test/auth-fixtures';

const drawerCopy = copy.shell.drawer;
const modules = drawerCopy.modules;

type TestRole = 'user' | 'minister' | 'ops' | 'admin';

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location-path">{location.pathname}</output>;
}

async function renderApp(path: string, role: TestRole = 'user', adminApi?: AdminApi) {
  const store = await createAuthedStore(testUser({ role }));
  renderWithShell(
    <>
      <AppRoutes />
      <LocationProbe />
    </>,
    store,
    [path],
    { adminApi },
  );
  return screen.getByTestId('location-path');
}

describe('抽屉开合与 URL 同步', () => {
  it('/settings 打开抽屉到个人段顶层：段标签、四模块、顶层占位', async () => {
    await renderApp('/settings');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    expect(within(dialog).getByText(drawerCopy.personalSegmentLabel)).toBeInTheDocument();
    for (const name of [modules.profile, modules.security, modules.appearance, modules.knowledge]) {
      expect(within(dialog).getByRole('button', { name })).toBeInTheDocument();
    }
    expect(within(dialog).getByText(drawerCopy.topPlaceholderBody)).toBeInTheDocument();
    // 普通用户无管理段
    expect(within(dialog).queryByText(drawerCopy.adminSegmentLabel)).not.toBeInTheDocument();
  });

  it('/ 与未知路径不打开抽屉', async () => {
    await renderApp('/');
    await screen.findByLabelText(copy.chat.composer.inputPlaceholder);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('深链 /settings/knowledge/uploads 刷新式恢复到对应层', async () => {
    await renderApp('/settings/knowledge/uploads');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    // 左栏第一位为当前层名，上方为返回上一层按钮
    expect(within(dialog).getByText(modules.uploads)).toBeInTheDocument();
    expect(
      within(dialog).getByRole('button', { name: drawerCopy.backAria(modules.knowledge) }),
    ).toBeInTheDocument();
    // uploads 层渲染真实上传结果内容（空态；fake api 无任务）
    expect(
      await within(dialog).findByText(copy.settings.knowledge.uploads.empty),
    ).toBeInTheDocument();
  });

  it('未注册层深链落抽屉首层占位（规格 §3）', async () => {
    await renderApp('/settings/no-such-module');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    expect(within(dialog).getByText(drawerCopy.topPlaceholderBody)).toBeInTheDocument();
    expect(
      within(dialog).getByRole('button', { name: modules.knowledge }),
    ).toBeInTheDocument();
  });

  it('运维访问管理段顶层 /admin 自动选中「总览」（/admin/dashboard）', async () => {
    const probe = await renderApp('/admin', 'ops');
    const dialog = await screen.findByRole('dialog', { name: modules.dashboard });
    expect(dialog).toBeInTheDocument();
    await waitFor(() => expect(probe.textContent).toBe('/admin/dashboard'));
  });

  it('普通用户访问 /admin 时回到可访问路径且不保留抽屉', async () => {
    const probe = await renderApp('/admin');

    await waitFor(() => expect(probe.textContent).toBe('/'));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('普通用户访问 /admin/* 深链时回到可访问路径且不保留抽屉', async () => {
    const probe = await renderApp('/admin/users');

    await waitFor(() => expect(probe.textContent).toBe('/'));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });
});

describe('左栏按角色渲染与跨段切换', () => {
  it('运维：左栏渲染管理段六模块；从个人段点管理模块切到管理段', async () => {
    const probe = await renderApp('/settings', 'ops');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    expect(within(dialog).getByText(drawerCopy.adminSegmentLabel)).toBeInTheDocument();
    for (const name of [
      modules.dashboard,
      modules.approvals,
      modules.spaces,
      modules.evaluation,
      modules.operations,
      modules.usersOps,
    ]) {
      expect(within(dialog).getByRole('button', { name })).toBeInTheDocument();
    }
    const user = userEvent.setup();
    await user.click(within(dialog).getByRole('button', { name: modules.dashboard }));
    // 跨段导航到 /admin/dashboard，页级标题切换为当前模块名
    await screen.findByRole('dialog', { name: modules.dashboard });
    expect(probe.textContent).toBe('/admin/dashboard');
  });

  it('超管：管理段无审批中心，用户模块名为「人员与权限」', async () => {
    await renderApp('/settings', 'admin');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    expect(within(dialog).queryByRole('button', { name: modules.approvals })).not.toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: modules.usersAdmin })).toBeInTheDocument();
  });
});

describe('下钻、返回与 Esc 逐层', () => {
  it('点模块下钻到知识库内容，再经模块内入口下钻到「我的投稿」；返回按钮逐层回退', async () => {
    const probe = await renderApp('/settings');
    const user = userEvent.setup();
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    await user.click(within(dialog).getByRole('button', { name: modules.knowledge }));
    // 知识库层：渲染真实模块内容（配额计数器 + 我的投稿入口）
    expect(await within(dialog).findByText(copy.settings.knowledge.quota.title)).toBeInTheDocument();
    expect(probe.textContent).toBe('/settings/knowledge');
    await user.click(within(dialog).getByRole('button', { name: modules.submissions }));
    expect(await within(dialog).findByText(copy.settings.knowledge.submissions.title)).toBeInTheDocument();
    expect(probe.textContent).toBe('/settings/knowledge/submissions');
    // 返回按钮回到知识库层
    await user.click(
      within(dialog).getByRole('button', { name: drawerCopy.backAria(modules.knowledge) }),
    );
    expect(await within(dialog).findByText(copy.settings.knowledge.quota.title)).toBeInTheDocument();
    expect(probe.textContent).toBe('/settings/knowledge');
  });

  it('运维在知识库层不渲染「我的投稿」（无权限模块不渲染）', async () => {
    await renderApp('/settings/knowledge', 'ops');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    expect(await within(dialog).findByText(copy.settings.knowledge.quota.title)).toBeInTheDocument();
    expect(
      within(dialog).queryByRole('button', { name: modules.submissions }),
    ).not.toBeInTheDocument();
  });

  it('Esc 逐层向上：下钻层 → 上一层 → 抽屉顶层 → 关闭抽屉', async () => {
    const probe = await renderApp('/settings/knowledge/uploads');
    const user = userEvent.setup();
    await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    await user.keyboard('{Escape}');
    await waitFor(() => expect(probe.textContent).toBe('/settings/knowledge'));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(probe.textContent).toBe('/settings'));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(probe.textContent).toBe('/'));
    // 关闭动画（400ms --duration-slow）后抽屉卸载
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument(), {
      timeout: 2000,
    });
  });

  it('左上角关闭按钮关闭抽屉并回到聊天主页', async () => {
    const probe = await renderApp('/settings/knowledge');
    const user = userEvent.setup();
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    await user.click(within(dialog).getByRole('button', { name: drawerCopy.closeAria }));
    await waitFor(() => expect(probe.textContent).toBe('/'));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument(), {
      timeout: 2000,
    });
    // 聊天主页在抽屉下方保持挂载，关闭后立即呈现（输入区在场）
    expect(screen.getByLabelText(copy.chat.composer.inputPlaceholder)).toBeInTheDocument();
  });

  it('全屏抽屉圈定键盘焦点，并在关闭后恢复到打开控件', async () => {
    const originalOffsetParent = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetParent');
    Object.defineProperty(HTMLElement.prototype, 'offsetParent', {
      configurable: true,
      get: () => document.body,
    });

    try {
      await renderApp('/');
      const user = userEvent.setup();
      const opener = screen.getByRole('button', { name: copy.shell.home.openDrawerAria });
      opener.focus();
      await user.click(opener);

      const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
      const closeButton = within(dialog).getByRole('button', { name: drawerCopy.closeAria });
      const focusable = within(dialog).getAllByRole('button');
      const last = focusable[focusable.length - 1]!;
      expect(closeButton).toHaveFocus();

      last.focus();
      await user.keyboard('{Tab}');
      expect(closeButton).toHaveFocus();

      closeButton.focus();
      await user.keyboard('{Shift>}{Tab}{/Shift}');
      expect(last).toHaveFocus();

      await user.click(closeButton);
      expect(dialog.contains(document.activeElement)).toBe(true);
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument(), {
        timeout: 2000,
      });
      await waitFor(() => expect(opener).toHaveFocus());
    } finally {
      if (originalOffsetParent === undefined) {
        delete (HTMLElement.prototype as { offsetParent?: HTMLElement | null }).offsetParent;
      } else {
        Object.defineProperty(HTMLElement.prototype, 'offsetParent', originalOffsetParent);
      }
    }
  });

  it('下滑手势：跟手位移超过阈值即关闭（规格 §1）', async () => {
    const probe = await renderApp('/settings');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    const panel = dialog.querySelector('.drawer-panel');
    expect(panel).not.toBeNull();
    const { fireEvent } = await import('@testing-library/react');
    fireEvent.pointerDown(panel as Element, { clientY: 40 });
    fireEvent.pointerMove(panel as Element, { clientY: 400 });
    fireEvent.pointerUp(panel as Element, { clientY: 400 });
    await waitFor(() => expect(probe.textContent).toBe('/'));
  });
});

describe('抽屉页头铃铛与窄屏单栏化', () => {
  it('抽屉页头右侧挂铃铛（规格 §4；共用基座 §5.1）', async () => {
    await renderApp('/settings');
    const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
    expect(
      within(dialog).getByRole('button', { name: copy.notifications.bellAria }),
    ).toBeInTheDocument();
  });

  it('窄屏（<768px）：首屏模块名单栏整页，点模块整页下钻', async () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      matches: query.includes('max-width'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
    try {
      const probe = await renderApp('/settings', 'ops');
      const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
      // 单栏：桌面左栏 nav 不渲染，模块列表在内容区整页呈现
      expect(dialog.querySelector('nav')).toBeNull();
      expect(dialog.querySelector('[data-nav-variant="modules"]')).not.toBeNull();
      const user = userEvent.setup();
      await user.click(within(dialog).getByRole('button', { name: modules.knowledge }));
      expect(await within(dialog).findByText(copy.settings.knowledge.quota.title)).toBeInTheDocument();
      expect(probe.textContent).toBe('/settings/knowledge');
    } finally {
      window.matchMedia = original;
    }
  });
});

describe('prefers-reduced-motion 降级', () => {
  it('下钻降级为直出：无 FLIP 克隆节点，内容立即切换（共用基座 §5.2）', async () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      matches: query.includes('prefers-reduced-motion'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
    try {
      const probe = await renderApp('/settings');
      const user = userEvent.setup();
      const dialog = await screen.findByRole('dialog', { name: drawerCopy.personalTitle });
      await user.click(within(dialog).getByRole('button', { name: modules.knowledge }));
      // 直出：点击后的提交里内容已切换，无需等待动画时序
      expect(within(dialog).getByText(copy.settings.knowledge.quota.title)).toBeInTheDocument();
      expect(probe.textContent).toBe('/settings/knowledge');
      expect(document.querySelector('.drill-flip-clone')).toBeNull();
    } finally {
      window.matchMedia = original;
    }
  });
});

describe('左栏项右侧摘要（renderSummary：徽标 / 状态点）', () => {
  const openWindow = {
    window_id: 'cw_1',
    status: 'open' as const,
    opened_at: '2026-08-03T02:00:00Z',
    closed_at: null,
    pairs_collected: 12,
    close_deadline_at: null,
    window_kind: 'manual' as const,
    policy_version: 'eval_2026_v1',
    sample_rate: 0.1,
    opened_by: 'u_ops',
    closed_by: null,
  };

  it('管理段模块按钮：仅可靠的配额待审徽标 / 评测开窗状态点 / 系统运维超时琥珀徽标', async () => {
    const adminApi = fakeAdminApi({
      getApprovalSummary: vi.fn(async () => ({ quota_pending: 2, submission_pending: 1 })),
      getCalibrationWindow: vi.fn(async () => openWindow),
      listOpsJobs: vi.fn(async () => ({ items: [], stale_count: 3 })),
    });
    await renderApp('/admin', 'ops', adminApi);
    const dialog = await screen.findByRole('dialog', { name: modules.dashboard });
    // 审批中心：仅后端真实提供的配额待审数 2
    const approvalsButton = within(dialog).getByRole('button', { name: /审批中心/ });
    expect(await within(approvalsButton).findByText('2')).toBeInTheDocument();
    expect(within(approvalsButton).queryByText('3')).toBeNull();
    // 评测与校准：开窗中成功绿状态点
    const evaluationButton = within(dialog).getByRole('button', { name: new RegExp(modules.evaluation) });
    await waitFor(() => expect(evaluationButton.querySelector('.bg-success')).not.toBeNull());
    // 系统运维：stale_count 3 警告琥珀徽标
    const operationsButton = within(dialog).getByRole('button', { name: new RegExp(modules.operations) });
    expect(await within(operationsButton).findByText('3')).toBeInTheDocument();
    // 无摘要模块（总览 / 知识空间 / 用户管理）不渲染徽标
    const dashboardButton = within(dialog).getByRole('button', { name: modules.dashboard });
    expect(dashboardButton.querySelector('.bg-mist-gray')).toBeNull();
  });

  it('审批中心下钻行：仅配额申请显示可靠待审徽标', async () => {
    const adminApi = fakeAdminApi({
      getApprovalSummary: vi.fn(async () => ({ quota_pending: 2, submission_pending: 1 })),
    });
    await renderApp('/admin/approvals', 'ops', adminApi);
    const dialog = await screen.findByRole('dialog', { name: modules.approvals });
    const quotaRow = within(dialog).getByRole('button', { name: new RegExp(modules.quotaRequests) });
    expect(await within(quotaRow).findByText('2')).toBeInTheDocument();
    const submissionsRow = within(dialog).getByRole('button', {
      name: new RegExp(modules.knowledgeApprovals),
    });
    await waitFor(() => expect(adminApi.getApprovalSummary).toHaveBeenCalled());
    expect(within(submissionsRow).queryByText('1')).toBeNull();
  });

  it('超管投稿审核项不展示不可靠的投稿待审数', async () => {
    const adminApi = fakeAdminApi({
      getApprovalSummary: vi.fn(async () => ({ quota_pending: 2, submission_pending: 1 })),
    });
    await renderApp('/admin/spaces', 'admin', adminApi);
    const dialog = await screen.findByRole('dialog', { name: modules.spaces });
    const submissionsRow = within(dialog).getByRole('button', {
      name: new RegExp(modules.knowledgeApprovals),
    });
    expect(adminApi.getApprovalSummary).not.toHaveBeenCalled();
    expect(within(submissionsRow).queryByText('1')).toBeNull();
  });

  it('摘要为 0 不渲染：模块按钮保持原标题与布局', async () => {
    await renderApp('/admin', 'ops');
    const dialog = await screen.findByRole('dialog', { name: modules.dashboard });
    const approvalsButton = within(dialog).getByRole('button', { name: modules.approvals });
    // 默认 fake：合计 0 / closed / stale 0 —— 摘要静默，按钮可访问名即原标题
    expect(approvalsButton.textContent).toBe(modules.approvals);
  });
});
