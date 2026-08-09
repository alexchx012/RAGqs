import { describe, expect, it, vi } from 'vitest';
import type { ApiClient, AuthSessionGuard, JsonApiRequestOptions } from '../api/client';
import { createAuthApi } from './api';

/** JSON overload of ApiClient.request — avoids vi.fn collapsing to the Blob overload. */
type ApiRequestDouble = (path: string, options?: JsonApiRequestOptions) => Promise<unknown>;

function createClientDouble(options?: {
  captureImpl?: () => AuthSessionGuard;
}) {
  const authSessionGuard: AuthSessionGuard = { authSessionId: 'logical_a' };
  const captureAuthSessionGuard = vi.fn(options?.captureImpl ?? (() => authSessionGuard));
  const request = vi.fn<ApiRequestDouble>(async () => undefined);
  const client = {
    captureAuthSessionGuard,
    request,
  } as unknown as ApiClient;
  return { client, captureAuthSessionGuard, request, authSessionGuard };
}

describe('认证域 API 敏感 mutation 的 authSessionGuard', () => {
  it('createAuthApi 工厂创建时不调用 captureAuthSessionGuard', () => {
    const { client, captureAuthSessionGuard } = createClientDouble();

    createAuthApi(client);

    expect(captureAuthSessionGuard).not.toHaveBeenCalled();
  });

  it('logout：capture 一次，authSessionGuard 与 capture 返回值同一引用，且 capture 先于 request', async () => {
    const { client, captureAuthSessionGuard, request, authSessionGuard } = createClientDouble();
    const api = createAuthApi(client);

    await api.logout();

    expect(captureAuthSessionGuard).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith('/auth/logout', {
      method: 'POST',
      authSessionGuard,
    });
    expect(request.mock.calls[0]?.[1]).toMatchObject({ authSessionGuard });
    expect((request.mock.calls[0]?.[1] as { authSessionGuard: AuthSessionGuard }).authSessionGuard).toBe(
      authSessionGuard,
    );
    expect(captureAuthSessionGuard.mock.invocationCallOrder[0]!).toBeLessThan(
      request.mock.invocationCallOrder[0]!,
    );
  });

  it('revokeSession：capture 一次，authSessionGuard 与 capture 返回值同一引用', async () => {
    const { client, captureAuthSessionGuard, request, authSessionGuard } = createClientDouble();
    const api = createAuthApi(client);

    await api.revokeSession('sess_1');

    expect(captureAuthSessionGuard).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith('/auth/sessions/sess_1', {
      method: 'DELETE',
      authSessionGuard,
    });
    expect((request.mock.calls[0]?.[1] as { authSessionGuard: AuthSessionGuard }).authSessionGuard).toBe(
      authSessionGuard,
    );
    expect(captureAuthSessionGuard.mock.invocationCallOrder[0]!).toBeLessThan(
      request.mock.invocationCallOrder[0]!,
    );
  });

  it('revokeAllSessions：capture 一次，authSessionGuard 与 capture 返回值同一引用', async () => {
    const { client, captureAuthSessionGuard, request, authSessionGuard } = createClientDouble();
    const api = createAuthApi(client);

    await api.revokeAllSessions();

    expect(captureAuthSessionGuard).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith('/auth/sessions', {
      method: 'DELETE',
      authSessionGuard,
    });
    expect((request.mock.calls[0]?.[1] as { authSessionGuard: AuthSessionGuard }).authSessionGuard).toBe(
      authSessionGuard,
    );
    expect(captureAuthSessionGuard.mock.invocationCallOrder[0]!).toBeLessThan(
      request.mock.invocationCallOrder[0]!,
    );
  });

  it('同一 guarded wrapper 连续两次：每次 capture，第二次不同 guard 原样传入第二次 request', async () => {
    const guard1: AuthSessionGuard = { authSessionId: 'logical_a' };
    const guard2: AuthSessionGuard = { authSessionId: 'logical_b' };
    const captureAuthSessionGuard = vi
      .fn<() => AuthSessionGuard>()
      .mockReturnValueOnce(guard1)
      .mockReturnValueOnce(guard2);
    const request = vi.fn<ApiRequestDouble>(async () => undefined);
    const client = { captureAuthSessionGuard, request } as unknown as ApiClient;
    const api = createAuthApi(client);

    await api.logout();
    await api.logout();

    expect(captureAuthSessionGuard).toHaveBeenCalledTimes(2);
    expect(request).toHaveBeenCalledTimes(2);

    const firstOptions = request.mock.calls[0]?.[1] as { authSessionGuard: AuthSessionGuard };
    const secondOptions = request.mock.calls[1]?.[1] as { authSessionGuard: AuthSessionGuard };
    expect(firstOptions.authSessionGuard).toBe(guard1);
    expect(secondOptions.authSessionGuard).toBe(guard2);
    expect(secondOptions.authSessionGuard).not.toBe(guard1);

    expect(captureAuthSessionGuard.mock.invocationCallOrder[0]!).toBeLessThan(
      request.mock.invocationCallOrder[0]!,
    );
    expect(captureAuthSessionGuard.mock.invocationCallOrder[1]!).toBeLessThan(
      request.mock.invocationCallOrder[1]!,
    );
    expect(request.mock.invocationCallOrder[0]!).toBeLessThan(
      captureAuthSessionGuard.mock.invocationCallOrder[1]!,
    );
  });
});

describe('认证域 API listSessions 的 authSessionGuard（review Major 7）', () => {
  it('listSessions：capture 一次，guard 同一引用透传，且 capture 先于 request', async () => {
    const { client, captureAuthSessionGuard, request, authSessionGuard } = createClientDouble();
    request.mockResolvedValueOnce({ items: [{ id: 's1', device: 'd', last_active_at: 't', current: true }] });
    const api = createAuthApi(client);

    const result = await api.listSessions();

    expect(captureAuthSessionGuard).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith('/auth/sessions', { authSessionGuard });
    expect((request.mock.calls[0]?.[1] as { authSessionGuard: AuthSessionGuard }).authSessionGuard).toBe(
      authSessionGuard,
    );
    expect(captureAuthSessionGuard.mock.invocationCallOrder[0]!).toBeLessThan(
      request.mock.invocationCallOrder[0]!,
    );
    expect(result).toHaveLength(1);
  });

  it('每次调用都捕获当前逻辑会话 guard（切换后第二次捕获新 guard 透传）', async () => {
    const guard1: AuthSessionGuard = { authSessionId: 'logical_a' };
    const guard2: AuthSessionGuard = { authSessionId: 'logical_b' };
    const captureAuthSessionGuard = vi
      .fn<() => AuthSessionGuard>()
      .mockReturnValueOnce(guard1)
      .mockReturnValueOnce(guard2);
    const request = vi.fn<ApiRequestDouble>(async () => ({ items: [] }));
    const client = { captureAuthSessionGuard, request } as unknown as ApiClient;
    const api = createAuthApi(client);

    await api.listSessions();
    await api.listSessions();

    expect(captureAuthSessionGuard).toHaveBeenCalledTimes(2);
    expect(request.mock.calls[0]?.[1]).toMatchObject({ authSessionGuard: guard1 });
    expect(request.mock.calls[1]?.[1]).toMatchObject({ authSessionGuard: guard2 });
    expect((request.mock.calls[0]?.[1] as { authSessionGuard: AuthSessionGuard }).authSessionGuard).toBe(
      guard1,
    );
    expect((request.mock.calls[1]?.[1] as { authSessionGuard: AuthSessionGuard }).authSessionGuard).toBe(
      guard2,
    );
  });
});
