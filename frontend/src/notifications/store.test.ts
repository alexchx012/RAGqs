/*
 * 通知轮询层测试（shared-shell 规格 §4、§8）：start/stop 与 30s 轮询、
 * refreshUnread 失败静默、openPanel 状态机、markRead / markAllRead 语义。
 * 全部经 fakeNotificationsApi 注入，定时器用 vi.useFakeTimers 推进。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fakeNotificationsApi } from '../test/auth-fixtures';
import { NOTIFICATION_POLL_INTERVAL_MS, NotificationsStore } from './store';
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

/** 轮询回调内的 Promise 链走微任务；推进定时器后补几次微任务冲刷再断言状态。 */
async function flushMicrotasks(): Promise<void> {
  for (let index = 0; index < 10; index += 1) {
    await Promise.resolve();
  }
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('通知轮询层', () => {
  describe('start / stop', () => {
    it('start 立即拉取一次服务端权威未读数', async () => {
      const api = fakeNotificationsApi({ unreadCount: vi.fn(async () => ({ count: 7 })) });
      const store = new NotificationsStore(api);
      store.start();
      expect(api.unreadCount).toHaveBeenCalledTimes(1);
      await flushMicrotasks();
      expect(store.getState().unreadCount).toBe(7);
      store.dispose();
    });

    it('之后每 30 秒轮询一次', async () => {
      const api = fakeNotificationsApi();
      const store = new NotificationsStore(api);
      store.start();
      expect(api.unreadCount).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_INTERVAL_MS);
      expect(api.unreadCount).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_INTERVAL_MS * 2);
      expect(api.unreadCount).toHaveBeenCalledTimes(4);
      store.dispose();
    });

    it('重复 start 幂等，不叠加定时器', async () => {
      const api = fakeNotificationsApi();
      const store = new NotificationsStore(api);
      store.start();
      store.start();
      expect(api.unreadCount).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_INTERVAL_MS);
      expect(api.unreadCount).toHaveBeenCalledTimes(2);
      store.dispose();
    });

    it('stop 清空状态且不再轮询', async () => {
      const api = fakeNotificationsApi({ unreadCount: vi.fn(async () => ({ count: 3 })) });
      const store = new NotificationsStore(api);
      store.start();
      await store.openPanel();
      expect(store.getState().listStatus).toBe('ready');
      store.stop();
      expect(store.getState()).toEqual({ unreadCount: null, items: null, listStatus: 'idle' });
      await vi.advanceTimersByTimeAsync(NOTIFICATION_POLL_INTERVAL_MS * 2);
      expect(api.unreadCount).toHaveBeenCalledTimes(1); // 仅剩 start 立即那一次
    });
  });

  describe('refreshUnread', () => {
    it('失败时静默保持上次权威值，不抛错', async () => {
      const unreadCount = vi
        .fn<() => Promise<{ count: number }>>()
        .mockResolvedValueOnce({ count: 5 })
        .mockRejectedValueOnce(new Error('network down'));
      const api = fakeNotificationsApi({ unreadCount });
      const store = new NotificationsStore(api);
      await store.refreshUnread();
      expect(store.getState().unreadCount).toBe(5);
      await expect(store.refreshUnread()).resolves.toBeUndefined();
      expect(store.getState().unreadCount).toBe(5);
    });
  });

  describe('openPanel', () => {
    it('loading → ready，items 保持服务端顺序原样', async () => {
      const items = [makeItem({ id: 'a', title: 'title-a' }), makeItem({ id: 'b', title: 'title-b' })];
      const api = fakeNotificationsApi({ list: vi.fn(async () => ({ items })) });
      const store = new NotificationsStore(api);
      const pending = store.openPanel();
      expect(store.getState().listStatus).toBe('loading');
      await pending;
      expect(store.getState().listStatus).toBe('ready');
      expect(store.getState().items?.map((item) => item.id)).toEqual(['a', 'b']);
    });

    it('列表拉取失败 → listStatus error', async () => {
      const api = fakeNotificationsApi({
        list: vi.fn(async () => {
          throw new Error('boom');
        }),
      });
      const store = new NotificationsStore(api);
      await store.openPanel();
      expect(store.getState().listStatus).toBe('error');
    });
  });

  describe('markRead', () => {
    it('调用服务端标已读，本地同步该条并刷新未读数', async () => {
      const items = [makeItem({ id: 'a', read: false }), makeItem({ id: 'b', read: false })];
      const api = fakeNotificationsApi({
        list: vi.fn(async () => ({ items })),
        unreadCount: vi.fn(async () => ({ count: 1 })),
      });
      const store = new NotificationsStore(api);
      await store.openPanel();
      await store.markRead('a');
      expect(api.markRead).toHaveBeenCalledWith('a');
      const state = store.getState();
      expect(state.items?.find((item) => item.id === 'a')?.read).toBe(true);
      expect(state.items?.find((item) => item.id === 'b')?.read).toBe(false);
      expect(api.unreadCount).toHaveBeenCalledTimes(1);
      expect(state.unreadCount).toBe(1);
    });
  });

  describe('markAllRead', () => {
    it('无参数调用服务端 read-all；已渲染条目全已读，随后刷新计数并重新拉取列表', async () => {
      const unread = [makeItem({ id: 'a', read: false }), makeItem({ id: 'b', read: false })];
      const read = [makeItem({ id: 'a', read: true }), makeItem({ id: 'b', read: true })];
      const list = vi
        .fn<(limit?: number) => Promise<{ items: NotificationItem[] }>>()
        .mockResolvedValueOnce({ items: unread })
        .mockResolvedValue({ items: read });
      const api = fakeNotificationsApi({ list });
      const store = new NotificationsStore(api);
      await store.openPanel();
      expect(list).toHaveBeenCalledTimes(1);
      await store.markAllRead();
      expect(api.markAllRead).toHaveBeenCalledWith();
      expect(api.unreadCount).toHaveBeenCalledTimes(1);
      expect(list).toHaveBeenCalledTimes(2); // 随后重新 openPanel
      expect(store.getState().items?.every((item) => item.read)).toBe(true);
    });
  });
});
