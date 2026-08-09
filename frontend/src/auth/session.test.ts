import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/errors';
import type { AuthApi } from './api';
import { createMemoryAuthHub } from './channel';
import type { AuthBus, AuthBusListener, AuthBusMessage } from './channel';
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

function createManualAuthBus() {
  let listener!: AuthBusListener;
  const post = vi.fn();
  const bus: AuthBus = {
    post,
    subscribe(next) {
      listener = next;
      return () => undefined;
    },
    close: vi.fn(),
  };
  return {
    bus,
    post,
    deliver(message: AuthBusMessage) {
      listener(message);
    },
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

    it('密码修改已由服务端撤销全部会话时仅清理并广播，不再请求 DELETE /auth/sessions', async () => {
      const hub = createMemoryAuthHub();
      const apiA = fakeApi();
      const storeA = new AuthSessionStore({ api: apiA, bus: hub.createBus() });
      const storeB = new AuthSessionStore({ api: fakeApi(), bus: hub.createBus() });
      await storeA.login('zhangsan', 'password123');
      expect(storeB.getState().status).toBe('authenticated');

      storeA.handleServerAllSessionsRevoked();

      expect(apiA.revokeAllSessions).not.toHaveBeenCalled();
      expect(storeA.getState()).toEqual({ status: 'unauthenticated', token: null, user: null });
      expect(storeB.getState()).toEqual({ status: 'unauthenticated', token: null, user: null });
    });

    it('同会话 refresh 后，匹配的 all-sessions 收尾仍清理本地与 peer', async () => {
      const hub = createMemoryAuthHub();
      const storeA = new AuthSessionStore({ api: fakeApi(), bus: hub.createBus() });
      const storeB = new AuthSessionStore({ api: fakeApi(), bus: hub.createBus() });
      await storeA.login('zhangsan', 'password123');
      const sessionId = storeA.getAuthSessionId();
      expect(sessionId).not.toBeNull();

      await storeA.refresh();
      expect(storeA.getAuthSessionId()).toBe(sessionId);
      expect(storeB.getAuthSessionId()).toBe(sessionId);
      expect(storeA.getState().token).toBe('tok_refresh');

      storeA.handleServerAllSessionsRevoked(sessionId);

      expect(storeA.getState().status).toBe('unauthenticated');
      expect(storeB.getState().status).toBe('unauthenticated');
    });

    it('延迟的旧逻辑会话 all-sessions 事件不会清理已切换到 B 或新 A 的 peer', async () => {
      const hub = createMemoryAuthHub();
      const accountA = testUser();
      const accountB: User = {
        ...testUser(),
        id: 'u_2',
        username: 'lisi',
        display_name: '李四',
        real_name: '李四',
      };
      const apiA = fakeApi({
        login: vi.fn(async () => ({ token: 'tok_a1', user: accountA })),
      });
      const storeA = new AuthSessionStore({ api: apiA, bus: hub.createBus() });
      const storePeer = new AuthSessionStore({
        api: fakeApi({
          login: vi.fn(async () => ({ token: 'tok_b1', user: accountB })),
        }),
        bus: hub.createBus(),
      });
      await storeA.login('zhangsan', 'password123');
      const staleSessionId = storeA.getAuthSessionId();
      expect(staleSessionId).not.toBeNull();
      expect(storePeer.getState().status).toBe('authenticated');

      // Shared bus keeps tabs aligned: peer logout/login also moves storeA to B.
      await storePeer.logout();
      await storePeer.login('lisi', 'password123');
      expect(storePeer.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_b1',
        user: accountB,
      });
      expect(storeA.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_b1',
        user: accountB,
      });
      expect(storePeer.getAuthSessionId()).not.toBe(staleSessionId);
      expect(storeA.getAuthSessionId()).not.toBe(staleSessionId);

      // Delayed completion / redelivery for the old A identity must not clear the new session.
      storeA.handleServerAllSessionsRevoked(staleSessionId);

      expect(storeA.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_b1',
        user: accountB,
      });
      expect(storePeer.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_b1',
        user: accountB,
      });
    });

    it('本地已切换到新 A 会话时，捕获的旧 identity 收尾是 no-op', async () => {
      const accountA = testUser();
      let loginCalls = 0;
      const api = fakeApi({
        login: vi.fn(async () => {
          loginCalls += 1;
          return { token: `tok_a_${loginCalls}`, user: accountA };
        }),
      });
      const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
      await store.login('zhangsan', 'password123');
      const staleSessionId = store.getAuthSessionId();
      expect(staleSessionId).toBe('tok_a_1');

      await store.logout();
      await store.login('zhangsan', 'password123');
      expect(store.getAuthSessionId()).toBe('tok_a_2');

      store.handleServerAllSessionsRevoked(staleSessionId);

      expect(store.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_a_2',
        user: accountA,
      });
      expect(store.getAuthSessionId()).toBe('tok_a_2');
    });

    it('用户直接 revokeAllSessions 仍清理同一逻辑会话的 peer', async () => {
      const hub = createMemoryAuthHub();
      const storeA = new AuthSessionStore({ api: fakeApi(), bus: hub.createBus() });
      const storeB = new AuthSessionStore({ api: fakeApi(), bus: hub.createBus() });
      await storeA.login('zhangsan', 'password123');
      expect(storeB.getState().status).toBe('authenticated');

      await storeA.revokeAllSessions();

      expect(storeA.getState().status).toBe('unauthenticated');
      expect(storeB.getState().status).toBe('unauthenticated');
    });

    it('延迟完成的 direct revoke-all：切到 B 后不得清理 B，匹配 B 的 peer 也不清理', async () => {
      const hub = createMemoryAuthHub();
      const accountA = testUser();
      const accountB: User = {
        ...testUser(),
        id: 'u_2',
        username: 'lisi',
        display_name: '李四',
        real_name: '李四',
      };
      let releaseRevoke: () => void = () => {};
      const apiA = fakeApi({
        login: vi.fn(async () => ({ token: 'tok_a1', user: accountA })),
        revokeAllSessions: vi.fn(
          () =>
            new Promise<void>((resolve) => {
              releaseRevoke = resolve;
            }),
        ),
      });
      const storeA = new AuthSessionStore({ api: apiA, bus: hub.createBus() });
      const storePeer = new AuthSessionStore({
        api: fakeApi({
          login: vi.fn(async () => ({ token: 'tok_b1', user: accountB })),
        }),
        bus: hub.createBus(),
      });
      await storeA.login('zhangsan', 'password123');
      const staleSessionId = storeA.getAuthSessionId();
      expect(staleSessionId).toBe('tok_a1');

      const revokePromise = storeA.revokeAllSessions();
      expect(apiA.revokeAllSessions).toHaveBeenCalledTimes(1);

      // Shared bus: peer logout/login moves both tabs onto B before A's DELETE resolves.
      await storePeer.logout();
      await storePeer.login('lisi', 'password123');
      expect(storeA.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_b1',
        user: accountB,
      });
      expect(storePeer.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_b1',
        user: accountB,
      });
      expect(storeA.getAuthSessionId()).toBe('tok_b1');
      expect(storePeer.getAuthSessionId()).toBe('tok_b1');

      releaseRevoke();
      await revokePromise;

      expect(storeA.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_b1',
        user: accountB,
      });
      expect(storePeer.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_b1',
        user: accountB,
      });
      expect(storeA.getAuthSessionId()).toBe('tok_b1');
      expect(storePeer.getAuthSessionId()).toBe('tok_b1');
    });

    it('延迟完成的 direct revoke-all：logout→新 A 后旧完成不得清理新 A', async () => {
      const accountA = testUser();
      let loginCalls = 0;
      let releaseRevoke: () => void = () => {};
      const api = fakeApi({
        login: vi.fn(async () => {
          loginCalls += 1;
          return { token: `tok_a_${loginCalls}`, user: accountA };
        }),
        revokeAllSessions: vi.fn(
          () =>
            new Promise<void>((resolve) => {
              releaseRevoke = resolve;
            }),
        ),
      });
      const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
      await store.login('zhangsan', 'password123');
      expect(store.getAuthSessionId()).toBe('tok_a_1');

      const revokePromise = store.revokeAllSessions();
      expect(api.revokeAllSessions).toHaveBeenCalledTimes(1);

      await store.logout();
      await store.login('zhangsan', 'password123');
      expect(store.getAuthSessionId()).toBe('tok_a_2');
      expect(store.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_a_2',
        user: accountA,
      });

      releaseRevoke();
      await revokePromise;

      expect(store.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_a_2',
        user: accountA,
      });
      expect(store.getAuthSessionId()).toBe('tok_a_2');
    });

    it('延迟完成的 logout：切到 B 后不得清理 B', async () => {
      const hub = createMemoryAuthHub();
      const accountA = testUser();
      const accountB: User = {
        ...testUser(),
        id: 'u_2',
        username: 'lisi',
        display_name: '李四',
        real_name: '李四',
      };
      let releaseLogout: () => void = () => {};
      const apiA = fakeApi({
        login: vi.fn(async () => ({ token: 'tok_a1', user: accountA })),
        logout: vi.fn(
          () =>
            new Promise<void>((resolve) => {
              releaseLogout = resolve;
            }),
        ),
      });
      const storeA = new AuthSessionStore({ api: apiA, bus: hub.createBus() });
      const storePeer = new AuthSessionStore({
        api: fakeApi({
          login: vi.fn(async () => ({ token: 'tok_b1', user: accountB })),
        }),
        bus: hub.createBus(),
      });
      await storeA.login('zhangsan', 'password123');

      const logoutPromise = storeA.logout();
      expect(apiA.logout).toHaveBeenCalledTimes(1);

      await storePeer.logout();
      await storePeer.login('lisi', 'password123');
      expect(storeA.getAuthSessionId()).toBe('tok_b1');
      expect(storePeer.getAuthSessionId()).toBe('tok_b1');

      releaseLogout();
      await logoutPromise;

      expect(storeA.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_b1',
        user: accountB,
      });
      expect(storePeer.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_b1',
        user: accountB,
      });
    });

    it('延迟完成的 current-device revoke：切到 B 后不得清理 B', async () => {
      const hub = createMemoryAuthHub();
      const accountA = testUser();
      const accountB: User = {
        ...testUser(),
        id: 'u_2',
        username: 'lisi',
        display_name: '李四',
        real_name: '李四',
      };
      let releaseRevoke: () => void = () => {};
      const apiA = fakeApi({
        login: vi.fn(async () => ({ token: 'tok_a1', user: accountA })),
        revokeSession: vi.fn(
          () =>
            new Promise<void>((resolve) => {
              releaseRevoke = resolve;
            }),
        ),
      });
      const storeA = new AuthSessionStore({ api: apiA, bus: hub.createBus() });
      const storePeer = new AuthSessionStore({
        api: fakeApi({
          login: vi.fn(async () => ({ token: 'tok_b1', user: accountB })),
        }),
        bus: hub.createBus(),
      });
      await storeA.login('zhangsan', 'password123');

      const revokePromise = storeA.revokeSession('sess_self', { current: true });
      expect(apiA.revokeSession).toHaveBeenCalledTimes(1);

      await storePeer.logout();
      await storePeer.login('lisi', 'password123');
      expect(storeA.getAuthSessionId()).toBe('tok_b1');

      releaseRevoke();
      await revokePromise;

      expect(storeA.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_b1',
        user: accountB,
      });
      expect(storePeer.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_b1',
        user: accountB,
      });
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

  describe('延迟完成 identity fence 与 fail-closed current revoke', () => {
    function accountB(): User {
      return {
        ...testUser(),
        id: 'u_2',
        username: 'lisi',
        display_name: '李四',
        real_name: '李四',
      };
    }

    it('延迟 refresh 成功 A→B：返回旧 token 但不得覆盖 B 的 status/token/user/id', async () => {
      let releaseRefresh: (value: { token: string }) => void = () => {};
      const api = fakeApi({
        login: vi.fn(async (username: string) =>
          username === 'lisi'
            ? { token: 'tok_b1', user: accountB() }
            : { token: 'tok_a1', user: testUser() },
        ),
        refresh: vi.fn(
          () =>
            new Promise<{ token: string }>((resolve) => {
              releaseRefresh = resolve;
            }),
        ),
      });
      const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
      await store.login('zhangsan', 'password123');
      expect(store.getAuthSessionId()).toBe('tok_a1');

      const refreshPromise = store.refresh();
      expect(api.refresh).toHaveBeenCalledTimes(1);

      await store.login('lisi', 'password123');
      expect(store.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_b1',
        user: accountB(),
      });
      expect(store.getAuthSessionId()).toBe('tok_b1');

      releaseRefresh({ token: 'tok_a_stale' });
      await expect(refreshPromise).resolves.toBe('tok_a_stale');

      expect(store.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_b1',
        user: accountB(),
      });
      expect(store.getAuthSessionId()).toBe('tok_b1');
    });

    it('延迟 refresh 失败 A→B：rethrow 原错误且不得 clearAuth 清 B', async () => {
      let rejectRefresh: (reason?: unknown) => void = () => {};
      const api = fakeApi({
        login: vi.fn(async (username: string) =>
          username === 'lisi'
            ? { token: 'tok_b1', user: accountB() }
            : { token: 'tok_a1', user: testUser() },
        ),
        refresh: vi.fn(
          () =>
            new Promise<{ token: string }>((_resolve, reject) => {
              rejectRefresh = reject;
            }),
        ),
      });
      const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
      await store.login('zhangsan', 'password123');

      const refreshPromise = store.refresh();
      await store.login('lisi', 'password123');
      expect(store.getAuthSessionId()).toBe('tok_b1');

      const failure = authInvalid('invalid_refresh');
      rejectRefresh(failure);
      await expect(refreshPromise).rejects.toBe(failure);

      expect(store.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_b1',
        user: accountB(),
      });
      expect(store.getAuthSessionId()).toBe('tok_b1');
    });

    it('延迟 refresh 成功 same-account re-auth（A1→logout→A2）：旧成功不得覆盖 A2', async () => {
      const accountA = testUser();
      let loginCalls = 0;
      let releaseRefresh: (value: { token: string }) => void = () => {};
      const api = fakeApi({
        login: vi.fn(async () => {
          loginCalls += 1;
          return { token: `tok_a_${loginCalls}`, user: accountA };
        }),
        refresh: vi.fn(
          () =>
            new Promise<{ token: string }>((resolve) => {
              releaseRefresh = resolve;
            }),
        ),
      });
      const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
      await store.login('zhangsan', 'password123');
      expect(store.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_a_1',
        user: accountA,
      });
      expect(store.getAuthSessionId()).toBe('tok_a_1');

      const refreshPromise = store.refresh();
      expect(api.refresh).toHaveBeenCalledTimes(1);

      await store.logout();
      await store.login('zhangsan', 'password123');
      expect(store.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_a_2',
        user: accountA,
      });
      expect(store.getAuthSessionId()).toBe('tok_a_2');

      releaseRefresh({ token: 'tok_a_1_stale' });
      await expect(refreshPromise).resolves.toBe('tok_a_1_stale');

      expect(store.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_a_2',
        user: accountA,
      });
      expect(store.getAuthSessionId()).toBe('tok_a_2');
    });

    it('延迟 refresh 失败 same-account re-auth（A1→logout→A2）：rethrow 且 A2 保持 authenticated', async () => {
      const accountA = testUser();
      let loginCalls = 0;
      let rejectRefresh: (reason?: unknown) => void = () => {};
      const api = fakeApi({
        login: vi.fn(async () => {
          loginCalls += 1;
          return { token: `tok_a_${loginCalls}`, user: accountA };
        }),
        refresh: vi.fn(
          () =>
            new Promise<{ token: string }>((_resolve, reject) => {
              rejectRefresh = reject;
            }),
        ),
      });
      const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
      await store.login('zhangsan', 'password123');
      expect(store.getAuthSessionId()).toBe('tok_a_1');

      const refreshPromise = store.refresh();
      expect(api.refresh).toHaveBeenCalledTimes(1);

      await store.logout();
      await store.login('zhangsan', 'password123');
      expect(store.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_a_2',
        user: accountA,
      });
      expect(store.getAuthSessionId()).toBe('tok_a_2');

      const failure = authInvalid('session_revoked');
      rejectRefresh(failure);
      await expect(refreshPromise).rejects.toBe(failure);

      expect(store.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_a_2',
        user: accountA,
      });
      expect(store.getAuthSessionId()).toBe('tok_a_2');
    });

    it('未认证发起延迟 logout 后登录 B：旧完成不得 clearAuth 清 B', async () => {
      let releaseLogout: () => void = () => {};
      const api = fakeApi({
        login: vi.fn(async () => ({ token: 'tok_b1', user: accountB() })),
        logout: vi.fn(
          () =>
            new Promise<void>((resolve) => {
              releaseLogout = resolve;
            }),
        ),
      });
      const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
      expect(store.getAuthSessionId()).toBeNull();

      const logoutPromise = store.logout();
      expect(api.logout).toHaveBeenCalledTimes(1);

      await store.login('lisi', 'password123');
      expect(store.getAuthSessionId()).toBe('tok_b1');

      releaseLogout();
      await logoutPromise;

      expect(store.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_b1',
        user: accountB(),
      });
      expect(store.getAuthSessionId()).toBe('tok_b1');
    });

    it('未认证发起延迟 current revoke 后登录 B：不得清 B 且不广播无 id wildcard', async () => {
      const hub = createMemoryAuthHub();
      const received: AuthBusMessage[] = [];
      hub.createBus().subscribe((message) => received.push(message));

      let releaseRevoke: () => void = () => {};
      const api = fakeApi({
        login: vi.fn(async () => ({ token: 'tok_b1', user: accountB() })),
        revokeSession: vi.fn(
          () =>
            new Promise<void>((resolve) => {
              releaseRevoke = resolve;
            }),
        ),
      });
      const store = new AuthSessionStore({ api, bus: hub.createBus() });
      expect(store.getAuthSessionId()).toBeNull();

      const revokePromise = store.revokeSession('sess_self', { current: true });
      expect(api.revokeSession).toHaveBeenCalledTimes(1);

      await store.login('lisi', 'password123');
      expect(store.getAuthSessionId()).toBe('tok_b1');

      releaseRevoke();
      await revokePromise;

      expect(store.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_b1',
        user: accountB(),
      });
      expect(store.getAuthSessionId()).toBe('tok_b1');
      expect(received.filter((message) => message.type === 'session-revoked')).toEqual([]);
      expect(received.some((message) => message.type === 'login')).toBe(true);
    });

    it('自定义 bus 注入缺 id / 非 string / mismatch 的 current revoke 保持 B authenticated', async () => {
      let deliver!: AuthBusListener;
      const bus: AuthBus = {
        post: vi.fn(),
        subscribe(listener) {
          deliver = listener;
          return () => undefined;
        },
        close: vi.fn(),
      };
      const store = new AuthSessionStore({
        api: fakeApi({
          login: vi.fn(async () => ({ token: 'tok_b1', user: accountB() })),
        }),
        bus,
      });
      await store.login('lisi', 'password123');
      const matchingId = store.getAuthSessionId();
      expect(matchingId).toBe('tok_b1');

      deliver({ type: 'session-revoked', id: 'sess_1', current: true } as unknown as AuthBusMessage);
      expect(store.getState().status).toBe('authenticated');
      expect(store.getAuthSessionId()).toBe(matchingId);

      deliver({
        type: 'session-revoked',
        id: 'sess_1',
        current: true,
        authSessionId: 42,
      } as unknown as AuthBusMessage);
      expect(store.getState().status).toBe('authenticated');
      expect(store.getAuthSessionId()).toBe(matchingId);

      deliver({
        type: 'session-revoked',
        id: 'sess_1',
        current: true,
        authSessionId: '',
      } as unknown as AuthBusMessage);
      expect(store.getState().status).toBe('authenticated');
      expect(store.getAuthSessionId()).toBe(matchingId);

      deliver({
        type: 'session-revoked',
        id: 'sess_1',
        current: true,
        authSessionId: 'tok_other',
      });
      expect(store.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_b1',
        user: accountB(),
      });
      expect(store.getAuthSessionId()).toBe(matchingId);
    });

    it('自定义 bus 仅 matching non-empty authSessionId 的 current revoke 会清本地', async () => {
      let deliver!: AuthBusListener;
      const bus: AuthBus = {
        post: vi.fn(),
        subscribe(listener) {
          deliver = listener;
          return () => undefined;
        },
        close: vi.fn(),
      };
      const store = new AuthSessionStore({
        api: fakeApi({
          login: vi.fn(async () => ({ token: 'tok_b1', user: accountB() })),
        }),
        bus,
      });
      await store.login('lisi', 'password123');
      const matchingId = store.getAuthSessionId();
      expect(matchingId).toBe('tok_b1');

      deliver({
        type: 'session-revoked',
        id: 'sess_1',
        current: true,
        authSessionId: 'tok_mismatch',
      });
      expect(store.getState().status).toBe('authenticated');
      expect(store.getAuthSessionId()).toBe(matchingId);

      deliver({
        type: 'session-revoked',
        id: 'sess_1',
        current: true,
        authSessionId: matchingId!,
      });
      expect(store.getState()).toEqual({ status: 'unauthenticated', token: null, user: null });
      expect(store.getAuthSessionId()).toBeNull();
    });
  });

  describe('lifecycle epoch refresh barriers（Task 1 RED）', () => {
    function accountB(): User {
      return {
        ...testUser(),
        id: 'u_2',
        username: 'lisi',
        display_name: '李四',
        real_name: '李四',
      };
    }

    it('手工投递的 old-A refresh 在 A→B 后不得覆盖 B', async () => {
      const { bus, deliver } = createManualAuthBus();
      const userB = accountB();
      const api = fakeApi({
        login: vi.fn(async (username: string) =>
          username === 'lisi'
            ? { token: 'tok_b', user: userB }
            : { token: 'tok_a', user: testUser() },
        ),
      });
      const store = new AuthSessionStore({ api, bus });

      await store.login('zhangsan', 'password123');
      expect(store.getAuthSessionId()).toBe('tok_a');
      await store.login('lisi', 'password123');
      expect(store.getState()).toEqual({ status: 'authenticated', token: 'tok_b', user: userB });
      expect(store.getAuthSessionId()).toBe('tok_b');

      deliver({ type: 'refresh', token: 'tok_a_rotated', authSessionId: 'tok_a' });

      expect(store.getState()).toEqual({ status: 'authenticated', token: 'tok_b', user: userB });
      expect(store.getAuthSessionId()).toBe('tok_b');
    });

    it('手工投递 matching B refresh 只更新 B token，保留 B id/user', async () => {
      const { bus, deliver } = createManualAuthBus();
      const userB = accountB();
      const store = new AuthSessionStore({
        api: fakeApi({
          login: vi.fn(async () => ({ token: 'tok_b', user: userB })),
        }),
        bus,
      });
      await store.login('lisi', 'password123');

      deliver({ type: 'refresh', token: 'tok_b_rotated', authSessionId: 'tok_b' });

      expect(store.getState()).toEqual({ status: 'authenticated', token: 'tok_b_rotated', user: userB });
      expect(store.getAuthSessionId()).toBe('tok_b');
    });

    it('unknown 初始状态手工收到有效 refresh 时可接受', async () => {
      const { bus, deliver } = createManualAuthBus();
      const api = fakeApi();
      const store = new AuthSessionStore({ api, bus });
      expect(store.getState()).toEqual({ status: 'unknown', token: null, user: null });

      deliver({ type: 'refresh', token: 'tok_initial_rotated', authSessionId: 'tok_initial' });
      await Promise.resolve();

      expect(store.getState()).toEqual({
        status: 'authenticated',
        token: 'tok_initial_rotated',
        user: testUser(),
      });
      expect(store.getAuthSessionId()).toBe('tok_initial');
      expect(api.me).toHaveBeenCalledTimes(1);
    });

    it('post-logout unauthenticated 状态手工收到 refresh 时必须忽略', async () => {
      const { bus, deliver } = createManualAuthBus();
      const api = fakeApi();
      const store = new AuthSessionStore({ api, bus });
      await store.logout();
      expect(store.getState()).toEqual({ status: 'unauthenticated', token: null, user: null });
      expect(store.getAuthSessionId()).toBeNull();

      deliver({ type: 'refresh', token: 'tok_after_logout', authSessionId: 'tok_old' });

      expect(store.getState()).toEqual({ status: 'unauthenticated', token: null, user: null });
      expect(store.getAuthSessionId()).toBeNull();
      expect(api.me).not.toHaveBeenCalled();
    });

    it('A/B refresh flight 按生命周期隔离，old finally 不得清 B flight', async () => {
      const refreshResolvers: Array<(value: { token: string }) => void> = [];
      const userB = accountB();
      const api = fakeApi({
        login: vi.fn(async (username: string) =>
          username === 'lisi'
            ? { token: 'tok_b', user: userB }
            : { token: 'tok_a', user: testUser() },
        ),
        refresh: vi.fn(
          () =>
            new Promise<{ token: string }>((resolve) => {
              refreshResolvers.push(resolve);
            }),
        ),
      });
      const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
      await store.login('zhangsan', 'password123');

      const flightA = store.refresh();
      expect(api.refresh).toHaveBeenCalledTimes(1);

      await store.login('lisi', 'password123');
      const flightBFirst = store.refresh();
      const flightBSecond = store.refresh();
      expect(api.refresh).toHaveBeenCalledTimes(2);
      expect(refreshResolvers).toHaveLength(2);

      refreshResolvers[0]!({ token: 'tok_a_rotated' });
      await expect(flightA).resolves.toBe('tok_a_rotated');
      expect(store.getState()).toEqual({ status: 'authenticated', token: 'tok_b', user: userB });
      expect(store.getAuthSessionId()).toBe('tok_b');

      const flightBThird = store.refresh();
      expect(api.refresh).toHaveBeenCalledTimes(2);

      refreshResolvers[1]!({ token: 'tok_b_rotated' });
      await expect(Promise.all([flightBFirst, flightBSecond, flightBThird])).resolves.toEqual([
        'tok_b_rotated',
        'tok_b_rotated',
        'tok_b_rotated',
      ]);
      expect(store.getState()).toEqual({ status: 'authenticated', token: 'tok_b_rotated', user: userB });
      expect(store.getAuthSessionId()).toBe('tok_b');
    });

    it('bootstrap deferred refresh 在 logout 后成功不得复活、广播或加载 me', async () => {
      let releaseRefresh!: (value: { token: string }) => void;
      const { bus, post } = createManualAuthBus();
      const api = fakeApi({
        refresh: vi.fn(
          () =>
            new Promise<{ token: string }>((resolve) => {
              releaseRefresh = resolve;
            }),
        ),
      });
      const store = new AuthSessionStore({ api, bus });

      const bootstrapPromise = store.bootstrap();
      expect(api.refresh).toHaveBeenCalledTimes(1);
      await store.logout();
      expect(store.getState()).toEqual({ status: 'unauthenticated', token: null, user: null });
      expect(store.getAuthSessionId()).toBeNull();

      releaseRefresh({ token: 'tok_bootstrap_stale' });
      await bootstrapPromise;

      expect(store.getState()).toEqual({ status: 'unauthenticated', token: null, user: null });
      expect(store.getAuthSessionId()).toBeNull();
      expect(post).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'refresh' }));
      expect(api.me).not.toHaveBeenCalled();
    });

    it('bootstrap deferred refresh 在 current revoke 后成功不得复活或加载 user', async () => {
      let releaseRefresh!: (value: { token: string }) => void;
      const { bus } = createManualAuthBus();
      const api = fakeApi({
        refresh: vi.fn(
          () =>
            new Promise<{ token: string }>((resolve) => {
              releaseRefresh = resolve;
            }),
        ),
      });
      const store = new AuthSessionStore({ api, bus });

      const bootstrapPromise = store.bootstrap();
      expect(api.refresh).toHaveBeenCalledTimes(1);
      await store.revokeSession('sess_self', { current: true });
      expect(store.getState()).toEqual({ status: 'unauthenticated', token: null, user: null });
      expect(store.getAuthSessionId()).toBeNull();

      releaseRefresh({ token: 'tok_bootstrap_revoked' });
      await bootstrapPromise;

      expect(store.getState()).toEqual({ status: 'unauthenticated', token: null, user: null });
      expect(store.getAuthSessionId()).toBeNull();
      expect(api.me).not.toHaveBeenCalled();
    });

    it('accepted initial remote refresh 的延迟 me 在 B login 后不得污染 B', async () => {
      let releaseProfile!: (value: User) => void;
      const { bus, deliver } = createManualAuthBus();
      const userB = accountB();
      const api = fakeApi({
        login: vi.fn(async () => ({ token: 'tok_b', user: userB })),
        me: vi.fn(
          () =>
            new Promise<User>((resolve) => {
              releaseProfile = resolve;
            }),
        ),
      });
      const store = new AuthSessionStore({ api, bus });

      deliver({ type: 'refresh', token: 'tok_a_rotated', authSessionId: 'tok_a' });
      expect(api.me).toHaveBeenCalledTimes(1);
      expect(store.getState()).toEqual({ status: 'authenticated', token: 'tok_a_rotated', user: null });
      expect(store.getAuthSessionId()).toBe('tok_a');

      await store.login('lisi', 'password123');
      expect(store.getState()).toEqual({ status: 'authenticated', token: 'tok_b', user: userB });
      expect(store.getAuthSessionId()).toBe('tok_b');

      releaseProfile(testUser());
      await Promise.resolve();

      expect(store.getState()).toEqual({ status: 'authenticated', token: 'tok_b', user: userB });
      expect(store.getAuthSessionId()).toBe('tok_b');
    });
  });

  describe('lifecycle reentrancy / malformed bus barriers（RED）', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    /** One-shot: on the next authenticated snapshot, revoke all local sessions synchronously. */
    function armRevokeOnAuthenticated(store: AuthSessionStore): void {
      let armed = true;
      store.subscribe(() => {
        if (!armed) {
          return;
        }
        if (store.getState().status === 'authenticated') {
          armed = false;
          store.handleServerAllSessionsRevoked();
        }
      });
    }

    async function flushMicrotasks(): Promise<void> {
      await Promise.resolve();
      await Promise.resolve();
    }

    it('local bootstrap refresh 的 setState 重入 revoke 后不得残留 refresh 广播/定时器或调用 me', async () => {
      const { bus, post } = createManualAuthBus();
      const api = fakeApi({
        refresh: vi.fn(async () => ({ token: 'tok_bootstrap_reenter' })),
      });
      const store = new AuthSessionStore({ api, bus });
      armRevokeOnAuthenticated(store);

      await store.bootstrap();
      await flushMicrotasks();

      expect(store.getState()).toEqual({ status: 'unauthenticated', token: null, user: null });
      expect(store.getAuthSessionId()).toBeNull();
      expect(post).not.toHaveBeenCalledWith(expect.objectContaining({ type: 'refresh' }));
      expect(api.me).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(14 * 60_000);
      // Only the bootstrap refresh itself; no post-transition auto-refresh timer may fire.
      expect(api.refresh).toHaveBeenCalledTimes(1);
    });

    it('inbound unknown-session refresh 的 setState 重入 revoke 后不得残留定时器或调用 me', async () => {
      const { bus, deliver } = createManualAuthBus();
      const api = fakeApi();
      const store = new AuthSessionStore({ api, bus });
      expect(store.getState().status).toBe('unknown');
      armRevokeOnAuthenticated(store);

      deliver({ type: 'refresh', token: 'tok_peer_admit', authSessionId: 'sess_peer_admit' });
      await flushMicrotasks();

      expect(store.getState()).toEqual({ status: 'unauthenticated', token: null, user: null });
      expect(store.getAuthSessionId()).toBeNull();
      expect(api.me).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(14 * 60_000);
      expect(api.refresh).not.toHaveBeenCalled();
    });

    it('inbound same-authSessionId token 轮换 setState 重入 revoke 后不得残留定时器或调用 me', async () => {
      const { bus, deliver } = createManualAuthBus();
      const api = fakeApi({
        login: vi.fn(async () => ({ token: 'tok_same_sess', user: testUser() })),
      });
      const store = new AuthSessionStore({ api, bus });
      await store.login('zhangsan', 'password123');
      const sessionId = store.getAuthSessionId();
      expect(sessionId).toBe('tok_same_sess');
      expect(store.getState().user).toEqual(testUser());

      // Arm after login so only the inbound refresh transition triggers revoke.
      armRevokeOnAuthenticated(store);
      deliver({ type: 'refresh', token: 'tok_same_rotated', authSessionId: sessionId! });
      await flushMicrotasks();

      expect(store.getState()).toEqual({ status: 'unauthenticated', token: null, user: null });
      expect(store.getAuthSessionId()).toBeNull();
      expect(api.me).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(14 * 60_000);
      expect(api.refresh).not.toHaveBeenCalled();
    });

    /**
     * Deliver untyped bus payloads into the production onBusMessage path.
     * Casts stay test-local so AuthBusMessage's compile-time shape does not hide runtime gaps.
     */
    function deliverRaw(
      deliver: (message: AuthBusMessage) => void,
      payload: unknown,
    ): void {
      expect(() => deliver(payload as AuthBusMessage)).not.toThrow();
    }

    it('authenticated matching-id refresh 缺/空/非 string token 必须忽略，不得改状态或调用 me', async () => {
      const { bus, deliver } = createManualAuthBus();
      const api = fakeApi({
        login: vi.fn(async () => ({ token: 'tok_match_id_base', user: testUser() })),
      });
      const store = new AuthSessionStore({ api, bus });
      await store.login('zhangsan', 'password123');
      const baseline = store.getState();
      const matchingId = store.getAuthSessionId();
      expect(matchingId).toBe('tok_match_id_base');
      expect(baseline).toEqual({
        status: 'authenticated',
        token: 'tok_match_id_base',
        user: testUser(),
      });

      // Session id matches the live authenticated session so only credential validation can reject.
      deliverRaw(deliver, { type: 'refresh', authSessionId: matchingId });
      deliverRaw(deliver, { type: 'refresh', token: undefined, authSessionId: matchingId });
      deliverRaw(deliver, { type: 'refresh', token: '', authSessionId: matchingId });
      deliverRaw(deliver, { type: 'refresh', token: 99, authSessionId: matchingId });
      deliverRaw(deliver, { type: 'refresh', token: { nested: true }, authSessionId: matchingId });

      await flushMicrotasks();

      expect(store.getState()).toEqual(baseline);
      expect(store.getAuthSessionId()).toBe(matchingId);
      expect(api.me).not.toHaveBeenCalled();
      expect(api.refresh).not.toHaveBeenCalled();
    });

    it('unknown 会话 valid token 配缺/空/非 string authSessionId 必须忽略，不得准入', async () => {
      const { bus, deliver } = createManualAuthBus();
      const api = fakeApi();
      const store = new AuthSessionStore({ api, bus });
      expect(store.getState().status).toBe('unknown');
      expect(store.getAuthSessionId()).toBeNull();

      // Token is valid; only authSessionId shape/runtime validation may keep the session unknown.
      deliverRaw(deliver, { type: 'refresh', token: 'tok_should_not_admit' });
      deliverRaw(deliver, { type: 'refresh', token: 'tok_should_not_admit', authSessionId: undefined });
      deliverRaw(deliver, { type: 'refresh', token: 'tok_should_not_admit', authSessionId: '' });
      deliverRaw(deliver, { type: 'refresh', token: 'tok_should_not_admit', authSessionId: null });
      deliverRaw(deliver, { type: 'refresh', token: 'tok_should_not_admit', authSessionId: 7 });
      deliverRaw(deliver, {
        type: 'refresh',
        token: 'tok_should_not_admit',
        authSessionId: { id: 'obj' },
      });

      await flushMicrotasks();

      expect(store.getState()).toEqual({ status: 'unknown', token: null, user: null });
      expect(store.getAuthSessionId()).toBeNull();
      expect(api.me).not.toHaveBeenCalled();
      expect(api.refresh).not.toHaveBeenCalled();
    });

    it('手工投递畸形 auth-bus payload 不得抛错、改状态、动定时器或调用 me', async () => {
      const refreshDelayMs = 14 * 60_000;
      const advanceBeforeDeliveryMs = 5 * 60_000;
      const remainingToOriginalDeadlineMs = refreshDelayMs - advanceBeforeDeliveryMs;

      const { bus, deliver } = createManualAuthBus();
      const api = fakeApi({
        login: vi.fn(async () => ({ token: 'tok_malformed_base', user: testUser() })),
      });
      const store = new AuthSessionStore({ api, bus });
      await store.login('zhangsan', 'password123');
      const baseline = store.getState();
      const baselineSessionId = store.getAuthSessionId();
      expect(baseline).toEqual({
        status: 'authenticated',
        token: 'tok_malformed_base',
        user: testUser(),
      });
      expect(baselineSessionId).toBe('tok_malformed_base');

      // Advance before malformed delivery so a schedule-reset would miss the original deadline.
      await vi.advanceTimersByTimeAsync(advanceBeforeDeliveryMs);
      expect(api.refresh).not.toHaveBeenCalled();

      deliverRaw(deliver, null);
      deliverRaw(deliver, undefined);
      deliverRaw(deliver, 42);
      deliverRaw(deliver, 'not-an-object');
      deliverRaw(deliver, { notType: 'refresh' });
      deliverRaw(deliver, { type: 'refresh' });
      deliverRaw(deliver, { type: 'refresh', token: 99, authSessionId: baselineSessionId });
      deliverRaw(deliver, { type: 'refresh', token: '', authSessionId: baselineSessionId });
      deliverRaw(deliver, { type: 'refresh', token: 'tok', authSessionId: null });
      deliverRaw(deliver, { type: 'refresh', token: 'tok', authSessionId: 7 });
      deliverRaw(deliver, { type: 'refresh', token: 'tok', authSessionId: '' });

      await flushMicrotasks();

      expect(store.getState()).toEqual(baseline);
      expect(store.getAuthSessionId()).toBe(baselineSessionId);
      expect(api.me).not.toHaveBeenCalled();

      // Exactly the original login timer deadline. A reset/replaced timer would not fire here.
      await vi.advanceTimersByTimeAsync(remainingToOriginalDeadlineMs);
      expect(api.refresh).toHaveBeenCalledTimes(1);
      expect(store.getState().token).toBe('tok_refresh');
    });
  });

  describe('当前用户展示补丁', () => {
    it('仅更新已认证当前用户的允许展示字段，并通知订阅者使用新不可变快照', async () => {
      const store = new AuthSessionStore({ api: fakeApi(), bus: createMemoryAuthHub().createBus() });
      await store.login('zhangsan', 'password123');
      const previous = store.getState();
      const observedUsers: User[] = [];
      store.subscribe(() => {
        const observedUser = store.getState().user;
        if (observedUser !== null) {
          observedUsers.push(observedUser);
        }
      });

      const sync = store.createCurrentUserPresentationSync();
      sync({ display_name: '新显示名', avatar_url: '/avatars/u_1.png' });

      const current = store.getState();
      expect(current).toEqual({
        status: 'authenticated',
        token: 'tok_login',
        user: {
          ...testUser(),
          display_name: '新显示名',
          avatar_url: '/avatars/u_1.png',
        },
      });
      expect(current.user).not.toBe(previous.user);
      expect(previous.user).toEqual(testUser());
      expect(observedUsers).toEqual([current.user]);
    });

    it('未认证时展示补丁不创建 user 或通知订阅者', async () => {
      const store = new AuthSessionStore({ api: fakeApi(), bus: createMemoryAuthHub().createBus() });
      await store.logout();
      const listener = vi.fn();
      store.subscribe(listener);

      store.createCurrentUserPresentationSync()({ display_name: '不应写入' });

      expect(store.getState()).toEqual({ status: 'unauthenticated', token: null, user: null });
      expect(listener).not.toHaveBeenCalled();
    });

    it('旧账号捕获的展示补丁不能写入当前另一个账号', async () => {
      const accountA = testUser();
      const accountB: User = {
        ...testUser(),
        id: 'u_2',
        username: 'lisi',
        display_name: '李四',
        real_name: '李四',
      };
      let loginCalls = 0;
      const api = fakeApi({
        login: vi.fn(async () => {
          loginCalls += 1;
          return loginCalls === 1
            ? { token: 'tok_account_a', user: accountA }
            : { token: 'tok_account_b', user: accountB };
        }),
      });
      const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
      await store.login('zhangsan', 'password123');
      const syncAccountA = store.createCurrentUserPresentationSync();

      await store.logout();
      await store.login('lisi', 'password123');
      const listener = vi.fn();
      store.subscribe(listener);
      syncAccountA({ display_name: '来自账号 A 的迟到结果' });

      expect(store.getState().user).toEqual(accountB);
      expect(listener).not.toHaveBeenCalled();
    });

    it('同一用户登出后重新认证会使旧展示补丁失效', async () => {
      const accountA = testUser();
      let loginCalls = 0;
      const api = fakeApi({
        login: vi.fn(async () => {
          loginCalls += 1;
          return { token: `tok_account_a_${loginCalls}`, user: accountA };
        }),
      });
      const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
      await store.login('zhangsan', 'password123');
      const staleSync = store.createCurrentUserPresentationSync();

      await store.logout();
      await store.login('zhangsan', 'password123');
      const listener = vi.fn();
      store.subscribe(listener);
      staleSync({ display_name: '不应写入新会话' });

      expect(store.getState().user).toEqual(accountA);
      expect(listener).not.toHaveBeenCalled();
    });

    it('同一认证会话内普通 refresh 不会使展示补丁失效', async () => {
      const store = new AuthSessionStore({ api: fakeApi(), bus: createMemoryAuthHub().createBus() });
      await store.login('zhangsan', 'password123');
      const sync = store.createCurrentUserPresentationSync();

      await store.refresh();
      sync({ display_name: 'refresh 后仍有效' });

      expect(store.getState().user).toEqual({ ...testUser(), display_name: 'refresh 后仍有效' });
    });
  });
});
