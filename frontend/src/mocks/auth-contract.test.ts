import { afterEach, describe, expect, it, vi } from 'vitest';
import { resolveUrl } from '../api/client';
import { getMockCookie, REFRESH_COOKIE_NAME, setMockCookie } from './dev-cookies';
import { CSRF_COOKIE_NAME } from '../auth/cookies';

/*
 * 契约 mock 行为验证（规格 §1）：经 MSW 走真实 HTTP 边界，
 * 验证 Cookie 设置/清除、refresh 轮换与重用宽限、限流与四类认证失效码。
 */

async function post(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(resolveUrl(path), { method: 'POST', ...init });
}

async function loginOk(username = 'zhangsan', password = 'password123'): Promise<Response> {
  const response = await post('/v1/auth/login', {
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  expect(response.status).toBe(200);
  return response;
}

async function errorBody(response: Response): Promise<{
  error: { code: string; message: string; details: Record<string, unknown>; request_id: string };
}> {
  return (await response.json()) as never;
}

async function refreshRequest(csrfHeader: string | null = getMockCookie(CSRF_COOKIE_NAME)): Promise<Response> {
  return post('/v1/auth/refresh', {
    headers: csrfHeader === null ? {} : { 'X-CSRF-Token': csrfHeader },
  });
}

describe('契约 mock（规格 §1）', () => {
  describe('登录与 Cookie', () => {
    it('登录成功：返回 token + User，设置 refresh Cookie 与 CSRF Cookie', async () => {
      const response = await loginOk();
      const body = (await response.json()) as { token: string; user: { role: string } };
      expect(body.token).toBeTruthy();
      expect(body.user.role).toBe('user');
      expect(getMockCookie(REFRESH_COOKIE_NAME)).toBeTruthy();
      expect(getMockCookie(CSRF_COOKIE_NAME)).toBeTruthy();
    });

    it('登录失败：401 invalid_credentials，错误体 details 为对象、request_id 齐备', async () => {
      const response = await post('/v1/auth/login', {
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: 'zhangsan', password: 'wrong' }),
      });
      expect(response.status).toBe(401);
      const body = await errorBody(response);
      expect(body.error.code).toBe('invalid_credentials');
      expect(body.error.details).toEqual({});
      expect(body.error.request_id).toMatch(/^req_mock_/);
    });

    it('pending_delete 账号登录被拒（前端沿用清理凭证流程，无恢复入口）', async () => {
      const response = await post('/v1/auth/login', {
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: 'ghost', password: 'password123' }),
      });
      expect(response.status).toBe(401);
      expect((await errorBody(response)).error.code).toBe('invalid_credentials');
    });

    it('连续失败限流：429 too_many_attempts 并下发 retry_after_seconds', async () => {
      for (let attempt = 0; attempt < 5; attempt += 1) {
        const response = await post('/v1/auth/login', {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: 'zhangsan', password: 'wrong' }),
        });
        expect(response.status).toBe(401);
      }
      const throttled = await post('/v1/auth/login', {
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: 'zhangsan', password: 'password123' }),
      });
      expect(throttled.status).toBe(429);
      const body = await errorBody(throttled);
      expect(body.error.code).toBe('too_many_attempts');
      expect(Number(body.error.details['retry_after_seconds'])).toBeGreaterThan(0);
    });
  });

  describe('refresh 轮换与失效', () => {
    it('refresh 成功：返回新 access token 并轮换 refresh Cookie', async () => {
      await loginOk();
      const before = getMockCookie(REFRESH_COOKIE_NAME);
      const response = await refreshRequest();
      expect(response.status).toBe(200);
      const body = (await response.json()) as { token: string };
      expect(body.token).toBeTruthy();
      const after = getMockCookie(REFRESH_COOKIE_NAME);
      expect(after).toBeTruthy();
      expect(after).not.toBe(before);
    });

    it('5 秒内并发重用同一前驱：返回同一后继结果，不视为失败', async () => {
      await loginOk();
      const rt1 = getMockCookie(REFRESH_COOKIE_NAME) as string;
      const first = await refreshRequest();
      expect(first.status).toBe(200);
      const rt2 = getMockCookie(REFRESH_COOKIE_NAME) as string;
      expect(rt2).not.toBe(rt1);

      // 模拟并发标签页仍持前驱 rt1 发起 refresh
      setMockCookie(REFRESH_COOKIE_NAME, rt1);
      const replay = await refreshRequest();
      expect(replay.status).toBe(200);
      expect(getMockCookie(REFRESH_COOKIE_NAME)).toBe(rt2);
    });

    it('超过 5 秒重用已消费 token：refresh_reuse_detected 并撤销会话、清除 Cookie', async () => {
      await loginOk();
      const rt1 = getMockCookie(REFRESH_COOKIE_NAME) as string;
      expect((await refreshRequest()).status).toBe(200);

      setMockCookie(REFRESH_COOKIE_NAME, rt1);
      vi.useFakeTimers({ toFake: ['Date'] });
      try {
        vi.setSystemTime(Date.now() + 6_000);
        const replay = await refreshRequest();
        expect(replay.status).toBe(401);
        expect((await errorBody(replay)).error.code).toBe('refresh_reuse_detected');
        expect(getMockCookie(REFRESH_COOKIE_NAME)).toBeNull();

        // 会话已被撤销：再次 refresh 也是 invalid_refresh
        const again = await refreshRequest();
        expect(again.status).toBe(401);
        expect((await errorBody(again)).error.code).toBe('invalid_refresh');
      } finally {
        vi.useRealTimers();
      }
    });

    it('CSRF Cookie / 请求头不一致：403 csrf_failed', async () => {
      await loginOk();
      const response = await refreshRequest('csrf_forged');
      expect(response.status).toBe(403);
      expect((await errorBody(response)).error.code).toBe('csrf_failed');
    });

    it('无 Cookie 或未知 refresh token：401 invalid_refresh', async () => {
      const missing = await refreshRequest();
      expect(missing.status).toBe(401);
      expect((await errorBody(missing)).error.code).toBe('invalid_refresh');

      setMockCookie(REFRESH_COOKIE_NAME, 'mrt_unknown');
      setMockCookie(CSRF_COOKIE_NAME, 'mcsrf_unknown');
      const unknown = await refreshRequest();
      expect(unknown.status).toBe(401);
      expect((await errorBody(unknown)).error.code).toBe('invalid_refresh');
    });
  });

  describe('logout 与设备会话撤销', () => {
    it('logout：幂等 204，清除 Cookie，此后 refresh 失效', async () => {
      const login = await loginOk();
      const { token } = (await login.json()) as { token: string };
      const logout = await post('/v1/auth/logout', {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect(logout.status).toBe(204);
      expect(getMockCookie(REFRESH_COOKIE_NAME)).toBeNull();
      expect(getMockCookie(CSRF_COOKIE_NAME)).toBeNull();

      const refresh = await refreshRequest();
      expect(refresh.status).toBe(401);
      expect((await errorBody(refresh)).error.code).toBe('invalid_refresh');
    });

    it('GET /auth/sessions 列出设备并标记 current；撤销指定设备幂等 204', async () => {
      const first = await loginOk();
      const tokenA = (await first.json()) as { token: string };
      const second = await loginOk();
      const tokenB = (await second.json()) as { token: string };

      const list = await fetch(resolveUrl('/v1/auth/sessions'), {
        headers: { Authorization: `Bearer ${tokenA.token}` },
      });
      expect(list.status).toBe(200);
      const { items } = (await list.json()) as {
        items: Array<{ id: string; current: boolean }>;
      };
      expect(items).toHaveLength(2);
      const currentId = items.find((item) => item.current)?.id as string;
      const otherId = items.find((item) => !item.current)?.id as string;

      const revokeOther = await fetch(resolveUrl(`/v1/auth/sessions/${otherId}`), {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${tokenA.token}` },
      });
      expect(revokeOther.status).toBe(204);
      // 重复撤销幂等
      const revokeAgain = await fetch(resolveUrl(`/v1/auth/sessions/${otherId}`), {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${tokenA.token}` },
      });
      expect(revokeAgain.status).toBe(204);

      // 被撤销设备的 access token 再访问 → session_revoked
      const meRevoked = await fetch(resolveUrl('/v1/auth/me'), {
        headers: { Authorization: `Bearer ${tokenB.token}` },
      });
      expect(meRevoked.status).toBe(401);
      expect((await errorBody(meRevoked)).error.code).toBe('session_revoked');

      // 撤销当前设备：清除当前浏览器 Cookie（等同登出）
      const revokeCurrent = await fetch(resolveUrl(`/v1/auth/sessions/${currentId}`), {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${tokenA.token}` },
      });
      expect(revokeCurrent.status).toBe(204);
      expect(getMockCookie(REFRESH_COOKIE_NAME)).toBeNull();
    });

    it('DELETE /auth/sessions 退出全部设备：204 + 清除 Cookie，会话全部失效', async () => {
      const login = await loginOk();
      const { token } = (await login.json()) as { token: string };
      const revokeAll = await fetch(resolveUrl('/v1/auth/sessions'), {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      });
      expect(revokeAll.status).toBe(204);
      expect(getMockCookie(REFRESH_COOKIE_NAME)).toBeNull();

      const me = await fetch(resolveUrl('/v1/auth/me'), {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect(me.status).toBe(401);
      expect((await errorBody(me)).error.code).toBe('session_revoked');
    });
  });
});

afterEach(() => {
  vi.useRealTimers();
});
