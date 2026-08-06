/*
 * 测试辅助：构造认证层测试装配（内存总线 + 可覆盖的 fake AuthApi），
 * 以及经 AuthProvider + MemoryRouter 渲染的便捷入口。
 * renderWithShell 额外包共享壳层 provider（Esc 栈 / 抽屉注册表 / 通知轮询层），
 * 供 AppShell、DrawerHost、铃铛等壳层组件测试使用。
 */

import { render, type RenderResult } from '@testing-library/react';
import type { ReactElement } from 'react';
import { MemoryRouter, type InitialEntry } from 'react-router';
import { vi } from 'vitest';
import type { AuthApi } from '../auth/api';
import { AuthProvider } from '../auth/AuthProvider';
import { createMemoryAuthHub } from '../auth/channel';
import { AuthSessionStore } from '../auth/session';
import type { User } from '../auth/types';
import { EscStackProvider } from '../lib/esc-stack-provider';
import type { NotificationsApi } from '../notifications/api';
import { NotificationsProvider } from '../notifications/NotificationsProvider';
import { NotificationsStore } from '../notifications/store';
import { createDrawerRegistry, DrawerRegistryProvider } from '../shell/drawer/DrawerRegistryProvider';

export function testUser(overrides: Partial<User> = {}): User {
  return {
    id: 'u_1',
    username: 'zhangsan',
    display_name: 'zhangsan',
    real_name: 'zhangsan',
    department: { id: 'd_finance', name: 'Finance' },
    role: 'user',
    avatar_url: null,
    ...overrides,
  };
}

export function fakeAuthApi(overrides: Partial<AuthApi> = {}): AuthApi {
  return {
    login: vi.fn(async () => ({ token: 'tok_login', user: testUser() })),
    logout: vi.fn(async () => {}),
    refresh: vi.fn(async () => ({ token: 'tok_refresh' })),
    me: vi.fn(async () => testUser()),
    listSessions: vi.fn(async () => []),
    revokeSession: vi.fn(async () => {}),
    revokeAllSessions: vi.fn(async () => {}),
    ...overrides,
  };
}

export function createTestStore(api: AuthApi = fakeAuthApi()): AuthSessionStore {
  return new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
}

/** 已认证 store：bootstrap 完成（静默 refresh + 拉取档案）。 */
export async function createAuthedStore(user: User = testUser()): Promise<AuthSessionStore> {
  const store = createTestStore(
    fakeAuthApi({ login: vi.fn(async () => ({ token: 'tok_login', user })), me: vi.fn(async () => user) }),
  );
  await store.bootstrap();
  return store;
}

export function renderWithAuth(
  ui: ReactElement,
  store: AuthSessionStore,
  initialEntries: string[] = ['/'],
): RenderResult {
  return render(
    <AuthProvider store={store}>
      <MemoryRouter initialEntries={initialEntries}>{ui}</MemoryRouter>
    </AuthProvider>,
  );
}

/** fake 通知 API：默认空列表、未读 0；可按用例覆盖。 */
export function fakeNotificationsApi(overrides: Partial<NotificationsApi> = {}): NotificationsApi {
  return {
    list: vi.fn(async () => ({ items: [] })),
    unreadCount: vi.fn(async () => ({ count: 0 })),
    markRead: vi.fn(async () => {}),
    markAllRead: vi.fn(async () => {}),
    ack: vi.fn(async () => {}),
    ...overrides,
  };
}

/** 共享壳层装配渲染：AuthProvider + Esc 栈 + 抽屉注册表 + 通知轮询层。 */
export function renderWithShell(
  ui: ReactElement,
  store: AuthSessionStore,
  initialEntries: InitialEntry[] = ['/'],
  options: { notifications?: NotificationsStore } = {},
): RenderResult {
  const notifications = options.notifications ?? new NotificationsStore(fakeNotificationsApi());
  return render(
    <AuthProvider store={store}>
      <MemoryRouter initialEntries={initialEntries}>
        <EscStackProvider>
          <DrawerRegistryProvider registry={createDrawerRegistry()}>
            <NotificationsProvider store={notifications}>{ui}</NotificationsProvider>
          </DrawerRegistryProvider>
        </EscStackProvider>
      </MemoryRouter>
    </AuthProvider>,
  );
}
