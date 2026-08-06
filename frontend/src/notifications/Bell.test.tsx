/*
 * 铃铛组件测试（共用基座 §4）：未读徽标的显隐与 99+ 截断、可达性名称；
 * NotificationBell 组合（Popover）点击打开面板并拉取列表。
 * Bell 单测不需要 Esc provider；组合测试经 renderWithShell 装配。
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { createAuthedStore, fakeNotificationsApi, renderWithShell } from '../test/auth-fixtures';
import { Bell, NotificationBell } from './Bell';
import { NotificationsStore } from './store';

/** 构造一个已拉到指定未读数的 store（不经轮询，直接 refreshUnread 一次）。 */
async function storeWithUnread(count: number): Promise<NotificationsStore> {
  const store = new NotificationsStore(
    fakeNotificationsApi({ unreadCount: vi.fn(async () => ({ count })) }),
  );
  await store.refreshUnread();
  return store;
}

describe('铃铛未读徽标', () => {
  it('未读数为 null（未拉取过）时不显示徽标', () => {
    const store = new NotificationsStore(fakeNotificationsApi());
    const { container } = render(<Bell store={store} />);
    expect(container.querySelector('span')).toBeNull();
  });

  it('未读数为 0 时不显示徽标', async () => {
    const store = await storeWithUnread(0);
    const { container } = render(<Bell store={store} />);
    expect(container.querySelector('span')).toBeNull();
  });

  it('未读数 3：徽标显示数字并带对应 aria-label', async () => {
    const store = await storeWithUnread(3);
    render(<Bell store={store} />);
    const badge = screen.getByText('3');
    expect(badge).toHaveAttribute('aria-label', copy.notifications.unreadBadgeAria(3));
  });

  it('未读数 100：显示 99+，aria-label 用真实数值', async () => {
    const store = await storeWithUnread(100);
    render(<Bell store={store} />);
    const badge = screen.getByText('99+');
    expect(badge).toHaveAttribute('aria-label', copy.notifications.unreadBadgeAria(100));
  });
});

describe('铃铛按钮', () => {
  it('触发按钮存在且可达性名称来自文案常量', () => {
    const store = new NotificationsStore(fakeNotificationsApi());
    render(<Bell store={store} />);
    expect(screen.getByRole('button', { name: copy.notifications.bellAria })).toBeInTheDocument();
  });
});

describe('NotificationBell 组合', () => {
  it('点击铃铛打开面板并拉取列表', async () => {
    const api = fakeNotificationsApi();
    const notifications = new NotificationsStore(api);

    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <NotificationBell
          open={open}
          onOpenChange={setOpen}
          onNavigate={vi.fn()}
          store={notifications}
        />
      );
    }

    renderWithShell(<Harness />, await createAuthedStore(), ['/'], { notifications });
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: copy.notifications.bellAria }));
    expect(
      await screen.findByRole('heading', { name: copy.notifications.title }),
    ).toBeInTheDocument();
    expect(api.list).toHaveBeenCalledTimes(1);
  });
});
