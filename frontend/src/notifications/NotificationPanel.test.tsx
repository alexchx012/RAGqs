/*
 * 提醒下拉面板测试（共用基座 §4；契约 §5）：打开即拉列表与骨架、服务端顺序渲染、
 * 条目呈现（title / 相对时间 / 未读点）、点击标已读并跳转、未知类型兜底不导航、
 * 「全部已读」、空态与错误态重试。
 * 面板以 <Popover.Root open> 直挂渲染（Portal 内容落到 document.body），
 * Esc provider 由 renderWithShell 装配。
 */

import * as Popover from '@radix-ui/react-popover';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { createAuthedStore, fakeNotificationsApi, renderWithShell } from '../test/auth-fixtures';
import type { NotificationsApi } from './api';
import { NotificationPanel } from './NotificationPanel';
import { NotificationsStore } from './store';
import type { NotificationItem } from './types';

function makeItem(overrides: Partial<NotificationItem> = {}): NotificationItem {
  return {
    id: 'ntf_1',
    type: 'ingestion_completed',
    title: 'title-1',
    payload: { job_id: 'job_1', document_id: 'doc_1' },
    read: false,
    event_occurred_at: new Date().toISOString(),
    ...overrides,
  };
}

async function renderPanel(api: NotificationsApi) {
  const store = new NotificationsStore(api);
  const onNavigate = vi.fn();
  renderWithShell(
    <Popover.Root open>
      <Popover.Trigger>panel-anchor</Popover.Trigger>
      <NotificationPanel store={store} onNavigate={onNavigate} />
    </Popover.Root>,
    await createAuthedStore(),
    ['/'],
    { notifications: store },
  );
  return { store, onNavigate };
}

describe('提醒下拉面板', () => {
  it('打开即拉取列表，加载中显示骨架', async () => {
    const list = vi.fn<(limit?: number) => Promise<{ items: NotificationItem[] }>>(
      () => new Promise(() => {}), // 永不 resolve：停留在加载态
    );
    const api = fakeNotificationsApi({ list });
    await renderPanel(api);
    expect(list).toHaveBeenCalledTimes(1);
    expect(await screen.findByTestId('notification-skeleton-list')).toBeInTheDocument();
  });

  it('按服务端返回顺序渲染，前端不重排', async () => {
    const now = Date.now();
    const items = [
      makeItem({ id: 'a', title: 'title-A', event_occurred_at: new Date(now - 10 * 60_000).toISOString() }),
      makeItem({ id: 'b', title: 'title-B', event_occurred_at: new Date(now - 60_000).toISOString() }),
      makeItem({ id: 'c', title: 'title-C', event_occurred_at: new Date(now - 60 * 60_000).toISOString() }),
    ];
    const api = fakeNotificationsApi({ list: vi.fn(async () => ({ items })) });
    await renderPanel(api);
    const titles = (await screen.findAllByText(/^title-/)).map((element) => element.textContent);
    expect(titles).toEqual(['title-A', 'title-B', 'title-C']);
  });

  it('条目：title 原样显示、相对时间分档展示、未读条目带未读点', async () => {
    const now = Date.now();
    const items = [
      makeItem({
        id: 'u1',
        title: 'title-unread',
        read: false,
        event_occurred_at: new Date(now - 5 * 60_000).toISOString(),
      }),
      makeItem({
        id: 'r1',
        title: 'title-read',
        read: true,
        event_occurred_at: new Date(now - 2 * 3_600_000).toISOString(),
      }),
    ];
    const api = fakeNotificationsApi({ list: vi.fn(async () => ({ items })) });
    await renderPanel(api);
    expect(await screen.findByText('title-unread')).toBeInTheDocument();
    expect(screen.getByText('title-read')).toBeInTheDocument();
    expect(screen.getByText(copy.notifications.relative.minutes(5))).toBeInTheDocument();
    expect(screen.getByText(copy.notifications.relative.hours(2))).toBeInTheDocument();

    const dots = [...document.querySelectorAll('.notification-unread-dot')];
    expect(dots).toHaveLength(2);
    const dotOf = (title: string) =>
      dots.find((dot) => dot.closest('button')?.textContent?.includes(title));
    const unreadDot = dotOf('title-unread');
    const readDot = dotOf('title-read');
    expect(unreadDot).toBeDefined();
    expect(readDot).toBeDefined();
    expect(unreadDot as Element).toHaveClass('opacity-100');
    expect(readDot as Element).toHaveClass('opacity-0');
  });

  it('点击未读条目：标已读并跳转到该类型目标', async () => {
    const items = [makeItem({ id: 'ntf_click', title: 'title-click', read: false })];
    const api = fakeNotificationsApi({ list: vi.fn(async () => ({ items })) });
    const { onNavigate } = await renderPanel(api);
    const user = userEvent.setup();
    await user.click(await screen.findByText('title-click'));
    await waitFor(() => {
      expect(api.markRead).toHaveBeenCalledWith('ntf_click');
    });
    expect(onNavigate).toHaveBeenCalledWith('/settings/knowledge/uploads');
  });

  it('未知类型条目：通用兜底渲染，点击不导航、不崩溃', async () => {
    const items = [
      makeItem({ id: 'ntf_unknown', type: 'deep_research_completed', title: 'title-unknown', read: false }),
    ];
    const api = fakeNotificationsApi({ list: vi.fn(async () => ({ items })) });
    const { onNavigate } = await renderPanel(api);
    expect(await screen.findByText('title-unknown')).toBeInTheDocument();
    // 未知类型走 slate 着色的通用图标
    expect(document.querySelector('svg.text-slate-gray')).not.toBeNull();
    const user = userEvent.setup();
    await user.click(screen.getByText('title-unknown'));
    await waitFor(() => {
      expect(api.markRead).toHaveBeenCalledWith('ntf_unknown');
    });
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it('「全部已读」：调用 markAllRead，随后刷新未读数并重新拉取列表', async () => {
    const items = [makeItem({ id: 'a', title: 'title-x' }), makeItem({ id: 'b', title: 'title-y' })];
    const api = fakeNotificationsApi({ list: vi.fn(async () => ({ items })) });
    await renderPanel(api);
    await screen.findByText('title-x');
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: copy.notifications.readAll }));
    await waitFor(() => {
      expect(api.markAllRead).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(api.unreadCount).toHaveBeenCalledTimes(1);
      expect(api.list).toHaveBeenCalledTimes(2);
    });
  });

  it('空列表显示空态文案', async () => {
    const api = fakeNotificationsApi();
    await renderPanel(api);
    expect(await screen.findByText(copy.notifications.empty)).toBeInTheDocument();
  });

  it('列表失败显示错误态，点击重试重新拉取', async () => {
    const list = vi
      .fn<(limit?: number) => Promise<{ items: NotificationItem[] }>>()
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValue({ items: [] });
    const api = fakeNotificationsApi({ list });
    await renderPanel(api);
    expect(await screen.findByText(copy.notifications.error)).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: copy.notifications.retry }));
    expect(await screen.findByText(copy.notifications.empty)).toBeInTheDocument();
    expect(list).toHaveBeenCalledTimes(2);
  });
});
