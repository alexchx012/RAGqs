import { afterEach, describe, expect, it, vi } from 'vitest';
import { createAuth } from './create-auth';
import type { User } from './types';

function sampleUser(): User {
  return {
    id: 'u_1',
    username: 'zhangsan',
    display_name: '张三',
    real_name: '张三',
    department: null,
    role: 'user',
    avatar_url: null,
  };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('createAuth 装配：captureAuthSessionGuard ↔ store logical id', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('登录后 client.captureAuthSessionGuard 取得 store logical id；同会话 refresh 换 bearer 后 logical id 不变', async () => {
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const url = String(input);
      if (url.includes('/auth/login')) {
        return jsonResponse(200, { token: 'tok_login', user: sampleUser() });
      }
      if (url.includes('/auth/refresh')) {
        return jsonResponse(200, { token: 'tok_refresh' });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const { store, client } = createAuth();
    try {
      await store.login('zhangsan', 'password123');

      expect(store.getState().token).toBe('tok_login');
      const guardAfterLogin = client.captureAuthSessionGuard();
      expect(guardAfterLogin.authSessionId).toBe(store.getAuthSessionId());
      expect(guardAfterLogin.authSessionId).toBe('tok_login');

      await store.refresh();

      expect(store.getState().token).toBe('tok_refresh');
      const guardAfterRefresh = client.captureAuthSessionGuard();
      expect(guardAfterRefresh.authSessionId).toBe(store.getAuthSessionId());
      expect(guardAfterRefresh.authSessionId).toBe('tok_login');
      expect(guardAfterRefresh.authSessionId).not.toBe(store.getState().token);
    } finally {
      store.dispose();
    }
  });
});
