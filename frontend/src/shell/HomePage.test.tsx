/*
 * 聊天主页占位集成测试（fe-shared-shell 规格 §1、§4；共用基座 §3.1–§3.2、§5.1）。
 * 覆盖：侧边栏头像区按角色打开抽屉、运维登录落地标记自动展开管理段、
 * 抽屉开关心路中主页保持挂载（输入草稿原样保留）、主页右上角铃铛、
 * AppShell 通知轮询生命周期（仅已认证时运行）。
 */

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useLocation } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { NotificationsStore } from '../notifications/store';
import { AppRoutes } from '../router/AppRoutes';
import { AUTO_OPEN_ADMIN_DRAWER_STATE_KEY } from '../router/landing';
import {
  createAuthedStore,
  fakeNotificationsApi,
  renderWithShell,
  testUser,
} from '../test/auth-fixtures';

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location-path">{location.pathname}</output>;
}

describe('聊天主页占位（fe-shared-shell）', () => {
  it('普通用户点头像区 → 打开个人段「设置」抽屉', async () => {
    const store = await createAuthedStore();
    renderWithShell(<AppRoutes />, store, ['/']);
    const user = userEvent.setup();
    await screen.findByRole('heading', { name: copy.shell.placeholderTitle });
    await user.click(
      screen.getAllByRole('button', { name: copy.shell.home.openDrawerAria })[0] as HTMLElement,
    );
    expect(
      await screen.findByRole('dialog', { name: copy.shell.drawer.personalTitle }),
    ).toBeInTheDocument();
  });

  it('运维点头像区 → 打开管理段「总览」（共用基座 §3.2）', async () => {
    const store = await createAuthedStore(testUser({ role: 'ops', username: 'ops-wang' }));
    renderWithShell(<AppRoutes />, store, ['/']);
    const user = userEvent.setup();
    await screen.findByRole('heading', { name: copy.shell.placeholderTitle });
    await user.click(
      screen.getAllByRole('button', { name: copy.shell.home.openDrawerAria })[0] as HTMLElement,
    );
    expect(
      await screen.findByRole('dialog', { name: copy.shell.drawer.modules.dashboard }),
    ).toBeInTheDocument();
  });

  it('运维登录落地标记驱动抽屉自动展开到管理段首层（规格 §1）', async () => {
    const store = await createAuthedStore(testUser({ role: 'ops', username: 'ops-wang' }));
    renderWithShell(
      <>
        <AppRoutes />
        <LocationProbe />
      </>,
      store,
      [{ pathname: '/', state: { [AUTO_OPEN_ADMIN_DRAWER_STATE_KEY]: true } }],
    );
    expect(
      await screen.findByRole('dialog', { name: copy.shell.drawer.modules.dashboard }),
    ).toBeInTheDocument();
    // replace 消费标记后 URL 为 /admin/dashboard
    await waitFor(() =>
      expect(screen.getByTestId('location-path').textContent).toBe('/admin/dashboard'),
    );
  });

  it('抽屉开关心路中主页保持挂载：输入草稿原样保留（共用基座 §5.1）', async () => {
    const store = await createAuthedStore();
    renderWithShell(<AppRoutes />, store, ['/']);
    const user = userEvent.setup();
    const composer = await screen.findByRole('textbox', {
      name: copy.shell.home.composerPlaceholder,
    });
    await user.type(composer, '报销流程怎么走');
    await user.click(
      screen.getAllByRole('button', { name: copy.shell.home.openDrawerAria })[0] as HTMLElement,
    );
    const dialog = await screen.findByRole('dialog', { name: copy.shell.drawer.personalTitle });
    // 抽屉打开期间主页仍在下方挂载
    expect(composer).toHaveValue('报销流程怎么走');
    await user.click(within(dialog).getByRole('button', { name: copy.shell.drawer.closeAria }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument(), {
      timeout: 2000,
    });
    expect(
      screen.getByRole('textbox', { name: copy.shell.home.composerPlaceholder }),
    ).toHaveValue('报销流程怎么走');
  });

  it('主页右上角挂铃铛（共用基座 §3.1）', async () => {
    const store = await createAuthedStore();
    renderWithShell(<AppRoutes />, store, ['/']);
    await screen.findByRole('heading', { name: copy.shell.placeholderTitle });
    expect(
      screen.getByRole('button', { name: copy.notifications.bellAria }),
    ).toBeInTheDocument();
  });
});

describe('通知轮询生命周期（规格 §4：仅已认证时运行）', () => {
  it('认证壳层挂载即启动轮询（立即拉一次未读数），卸载停止', async () => {
    const unreadCount = vi.fn(async () => ({ count: 3 }));
    const notifications = new NotificationsStore(fakeNotificationsApi({ unreadCount }));
    const store = await createAuthedStore();
    const { unmount } = renderWithShell(<AppRoutes />, store, ['/'], { notifications });
    await waitFor(() => expect(notifications.getState().unreadCount).toBe(3));
    expect(unreadCount).toHaveBeenCalledTimes(1);
    unmount();
    expect(notifications.getState().unreadCount).toBeNull();
  });
});
