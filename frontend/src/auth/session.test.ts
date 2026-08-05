import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/errors';
import type { AuthApi } from './api';
import { createMemoryAuthHub } from './channel';
import { AuthSessionStore } from './session';
import type { User } from './types';

function testUser(role: User['role'] = 'user'): User {
  return {
    id: 'u_1',
    username: 'zhangsan',
    display_name: 'zhangsan',
    real_name: 'zhangsan',
    department: null,
    role,
    avatar_url: null,
  };
}

function fakeApi(overrides: Partial<AuthApi> = {}): AuthApi {
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

function authInvalid(code: string): ApiError {
  return new ApiError({ status: 401, code, message: '', details: {}, requestId: null });
}

describe('认证状态层（规格 §2–§3）', () => {
  describe('bootstrap 静默恢复', () => {
    it('refresh 成功 → 拉取用户档案 → authenticated', async () => {
      const api = fakeApi();
      const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
      await store.bootstrap();
      expect(api.refresh).toHaveBeenCalledTimes(1);
      expect(api.me).toHaveBeenCalledTimes(1);
      expect(store.getState()).toEqual({ status: 'authenticated', token: 'tok_refresh', user: testUser() });
    });

    it('refresh 失败 → unauthenticated，不拉取档案', async () => {
      const api = fakeApi({ refresh: vi.fn(async () => Promise.reject(authInvalid('invalid_refresh'))) });
      const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
      await store.bootstrap();
      expect(store.getState().status).toBe('unauthenticated');
      expect(store.getState().token).toBeNull();
      expect(api.me).not.toHaveBeenCalled();
    });

    it('幂等：重复 bootstrap 只 refresh 一次', async () => {
      const api = fakeApi();
      const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
      await Promise.all([store.bootstrap(), store.bootstrap()]);
      expect(api.refresh).toHaveBeenCalledTimes(1);
    });
  });

  describe('login / logout', () => {
    it('login 设置 authenticated 状态并广播 login', async () => {
      const hub = createMemoryAuthHub();
      const other = hub.createBus();
      const received: string[] = [];
      other.subscribe((message) => received.push(message.type));
      const store = new AuthSessionStore({ api: fakeApi(), bus: hub.createBus() });
      await store.login('zhangsan', 'password123');
      expect(store.getState().status).toBe('authenticated');
      expect(store.getState().token).toBe('tok_login');
      expect(received).toEqual(['login']);
    });

    it('logout 清理状态并广播；服务端失败不阻塞本地清理', async () => {
      const hub = createMemoryAuthHub();
      const received: string[] = [];
      hub.createBus().subscribe((message) => received.push(message.type));
      const api = fakeApi({ logout: vi.fn(async () => Promise.reject(new Error('down'))) });
      const store = new AuthSessionStore({ api, bus: hub.createBus() });
      await store.login('zhangsan', 'password123');
      await store.logout();
      expect(store.getState()).toEqual({ status: 'unauthenticated', token: null, user: null });
      expect(received).toEqual(['login', 'logout']);
    });
  });

  describe('refresh single-flight 与自动续期', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it('并发 refresh 等待同一次结果（single-flight）', async () => {
      let release: (value: { token: string }) => void = () => {};
      const api = fakeApi({
        refresh: vi.fn(
          () =>
            new Promise<{ token: string }>((resolve) => {
              release = resolve;
            }),
        ),
      });
      const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
      const first = store.refresh();
      const second = store.refresh();
      expect(api.refresh).toHaveBeenCalledTimes(1);
      release({ token: 'tok_sf' });
      await expect(first).resolves.toBe('tok_sf');
      await expect(second).resolves.toBe('tok_sf');
    });

    it('到期前自动 refresh（15 分钟 TTL，提前 60s）', async () => {
      const api = fakeApi();
      const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
      await store.login('zhangsan', 'password123');
      expect(api.refresh).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(14 * 60_000);
      expect(api.refresh).toHaveBeenCalledTimes(1);
      expect(store.getState().token).toBe('tok_refresh');
    });

    it('refresh 失败按认证失效处理：清理 token、停止自动 refresh', async () => {
      const api = fakeApi();
      const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
      await store.login('zhangsan', 'password123');
      api.refresh = vi.fn(async () => Promise.reject(authInvalid('refresh_reuse_detected')));
      await vi.advanceTimersByTimeAsync(14 * 60_000);
      expect(store.getState()).toEqual({ status: 'unauthenticated', token: null, user: null });
      await vi.advanceTimersByTimeAsync(60 * 60_000);
      expect(api.refresh).toHaveBeenCalledTimes(1);
    });
  });

  describe('四类认证失效码统一清理（含 pending_delete / deleted 沿用同一流程）', () => {
    it.each(['session_revoked', 'invalid_refresh', 'refresh_reuse_detected', 'csrf_failed'])(
      '%s → 清理内存 token 并进入未认证态',
      async (code) => {
        const api = fakeApi({
          refresh: vi.fn(async () => Promise.reject(authInvalid(code))),
        });
        const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
        await store.login('zhangsan', 'password123');
        await store.refresh().catch(() => undefined);
        expect(store.getState().status).toBe('unauthenticated');
        expect(store.getState().token).toBeNull();
      },
    );
  });

  describe('多标签页 BroadcastChannel 协调', () => {
    it('标签页 A login / refresh → 标签页 B 同步 token 与登录状态', async () => {
      const hub = createMemoryAuthHub();
      const apiA = fakeApi();
      const apiB = fakeApi();
      const storeA = new AuthSessionStore({ api: apiA, bus: hub.createBus() });
      const storeB = new AuthSessionStore({ api: apiB, bus: hub.createBus() });
      await storeA.login('zhangsan', 'password123');
      expect(storeB.getState().status).toBe('authenticated');
      expect(storeB.getState().token).toBe('tok_login');
      expect(storeB.getState().user).toEqual(testUser());

      await storeA.refresh();
      expect(storeB.getState().token).toBe('tok_refresh');
    });

    it('标签页 A 完成 refresh → B 采纳新 token 并在档案缺失时用 GET /auth/me 拉取', async () => {
      const hub = createMemoryAuthHub();
      const storeA = new AuthSessionStore({ api: fakeApi(), bus: hub.createBus() });
      const apiB = fakeApi();
      const storeB = new AuthSessionStore({ api: apiB, bus: hub.createBus() });
      await storeA.refresh();
      expect(storeB.getState().token).toBe('tok_refresh');
      await vi.waitFor(() => expect(apiB.me).toHaveBeenCalledTimes(1));
    });

    it('标签页 A logout / 撤销全部设备 → 标签页 B 同步回未认证态', async () => {
      const hub = createMemoryAuthHub();
      const storeA = new AuthSessionStore({ api: fakeApi(), bus: hub.createBus() });
      const storeB = new AuthSessionStore({ api: fakeApi(), bus: hub.createBus() });
      await storeA.login('zhangsan', 'password123');
      expect(storeB.getState().status).toBe('authenticated');
      await storeA.logout();
      expect(storeB.getState()).toEqual({ status: 'unauthenticated', token: null, user: null });

      await storeA.login('zhangsan', 'password123');
      await storeA.revokeAllSessions();
      expect(storeB.getState().status).toBe('unauthenticated');
    });

    it('指定设备撤销：current=true 清理，current=false 不影响本标签页', async () => {
      const hub = createMemoryAuthHub();
      const storeA = new AuthSessionStore({ api: fakeApi(), bus: hub.createBus() });
      const storeB = new AuthSessionStore({ api: fakeApi(), bus: hub.createBus() });
      await storeA.login('zhangsan', 'password123');

      await storeA.revokeSession('sess_other', { current: false });
      expect(storeB.getState().status).toBe('authenticated');
      expect(storeA.getState().status).toBe('authenticated');

      await storeA.revokeSession('sess_self', { current: true });
      expect(storeA.getState().status).toBe('unauthenticated');
      expect(storeB.getState().status).toBe('unauthenticated');
    });
  });
});
