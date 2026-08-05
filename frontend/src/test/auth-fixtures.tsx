/*
 * 测试辅助：构造认证层测试装配（内存总线 + 可覆盖的 fake AuthApi），
 * 以及经 AuthProvider + MemoryRouter 渲染的便捷入口。
 */

import { render, type RenderResult } from '@testing-library/react';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router';
import { vi } from 'vitest';
import type { AuthApi } from '../auth/api';
import { AuthProvider } from '../auth/AuthProvider';
import { createMemoryAuthHub } from '../auth/channel';
import { AuthSessionStore } from '../auth/session';
import type { User } from '../auth/types';

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
