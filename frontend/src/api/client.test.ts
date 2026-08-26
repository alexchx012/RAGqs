import { describe, expect, it, vi } from 'vitest';
import { createApiClient } from './client';
import { ApiError } from './errors';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function contractError(status: number, code: string, details: Record<string, unknown> = {}): Response {
  return jsonResponse(status, {
    error: { code, message: `msg:${code}`, details, request_id: 'req_test_1' },
  });
}

describe('API 客户端基座（规格 §2）', () => {
  it('请求带 /v1 前缀并携带 Authorization Bearer', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => jsonResponse(200, { ok: true }));
    const client = createApiClient({
      getAccessToken: () => 'tok_1',
      refresh: vi.fn(),
      fetchFn: fetchMock,
    });
    await client.request('/auth/me');
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain('/v1/auth/me');
    expect((init.headers as Record<string, string>)['Authorization']).toBe('Bearer tok_1');
    expect(init.credentials).toBe('include');
  });

  it('auth:false（登录与 refresh）不携带 Bearer，也无 Content-Type', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => jsonResponse(200, {}));
    const client = createApiClient({
      getAccessToken: () => 'tok_1',
      refresh: vi.fn(),
      fetchFn: fetchMock,
    });
    await client.request('/auth/refresh', { method: 'POST', auth: false });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(headers['Authorization']).toBeUndefined();
    expect(headers['Content-Type']).toBeUndefined();
  });

  it('错误归一化：code/message/details/request_id 齐备，details 始终为对象', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () =>
      contractError(429, 'too_many_attempts', { retry_after_seconds: 30 }),
    );
    const client = createApiClient({ getAccessToken: () => null, refresh: vi.fn(), fetchFn: fetchMock });
    const error = await client
      .request('/auth/login', { method: 'POST', auth: false, body: {} })
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(429);
    expect(apiError.code).toBe('too_many_attempts');
    expect(apiError.details).toEqual({ retry_after_seconds: 30 });
    expect(apiError.requestId).toBe('req_test_1');
  });

  it('非契约响应体兜底为 unknown_error 且 details 为 {}', async () => {
    const fetchMock = vi.fn<typeof fetch>(
      async () => new Response('<html>oops</html>', { status: 502 }),
    );
    const client = createApiClient({ getAccessToken: () => null, refresh: vi.fn(), fetchFn: fetchMock });
    const error = (await client.request('/x').catch((caught: unknown) => caught)) as ApiError;
    expect(error.code).toBe('unknown_error');
    expect(error.details).toEqual({});
  });

  it('2xx 的非 JSON 响应显式归一化为错误，而不是成功返回 null', async () => {
    const fetchMock = vi.fn<typeof fetch>(
      async () => new Response('<html>proxy error</html>', { status: 200 }),
    );
    const client = createApiClient({ getAccessToken: () => null, refresh: vi.fn(), fetchFn: fetchMock });

    const error = (await client.request('/x').catch((caught: unknown) => caught)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(200);
    expect(error.code).toBe('unknown_error');
  });

  it('业务请求 401：自动 refresh 一次后重试原请求（携带新 token）', async () => {
    let token: string | null = 'tok_old';
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockImplementationOnce(async () => contractError(401, 'invalid_token'))
      .mockImplementationOnce(async () => jsonResponse(200, { ok: true }));
    const refresh = vi.fn(async () => {
      token = 'tok_new';
      return 'tok_new';
    });
    const client = createApiClient({ getAccessToken: () => token, refresh, fetchFn: fetchMock });
    const result = await client.request<{ ok: boolean }>('/conversations');
    expect(result.ok).toBe(true);
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [, secondInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect((secondInit.headers as Record<string, string>)['Authorization']).toBe('Bearer tok_new');
  });

  it('auth:false 的 401 不触发 refresh（登录失败是表单错误）', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => contractError(401, 'invalid_credentials'));
    const refresh = vi.fn(async () => 'tok');
    const client = createApiClient({ getAccessToken: () => null, refresh, fetchFn: fetchMock });
    const error = (await client
      .request('/auth/login', { method: 'POST', auth: false, body: {} })
      .catch((caught: unknown) => caught)) as ApiError;
    expect(error.code).toBe('invalid_credentials');
    expect(refresh).not.toHaveBeenCalled();
  });

  it('refresh 后重试仍 401 则上抛，不再循环重试', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => contractError(401, 'session_revoked'));
    const refresh = vi.fn(async () => 'tok_new');
    const client = createApiClient({ getAccessToken: () => 'tok_old', refresh, fetchFn: fetchMock });
    const error = (await client.request('/auth/me').catch((caught: unknown) => caught)) as ApiError;
    expect(error.code).toBe('session_revoked');
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('refresh 失败按认证失效上抛 refresh 的错误', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => contractError(401, 'invalid_token'));
    const refreshFailure = new ApiError({
      status: 401,
      code: 'invalid_refresh',
      message: '',
      details: {},
      requestId: null,
    });
    const client = createApiClient({
      getAccessToken: () => 'tok',
      refresh: vi.fn(async () => {
        throw refreshFailure;
      }),
      fetchFn: fetchMock,
    });
    const error = (await client.request('/auth/me').catch((caught: unknown) => caught)) as ApiError;
    expect(error).toBe(refreshFailure);
  });

  it('网络层失败归一化为 network_error', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => {
      throw new TypeError('Failed to fetch');
    });
    const client = createApiClient({ getAccessToken: () => null, refresh: vi.fn(), fetchFn: fetchMock });
    const error = (await client.request('/x').catch((caught: unknown) => caught)) as ApiError;
    expect(error.code).toBe('network_error');
    expect(error.status).toBeNull();
  });

  it('客户端超时归一化为 timeout', async () => {
    const fetchMock = vi.fn<typeof fetch>(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () =>
            reject(new DOMException('The operation was aborted.', 'AbortError')),
          );
        }),
    );
    const client = createApiClient({
      getAccessToken: () => null,
      refresh: vi.fn(),
      fetchFn: fetchMock,
      timeoutMs: 10,
    });
    const error = (await client.request('/x').catch((caught: unknown) => caught)) as ApiError;
    expect(error.code).toBe('timeout');
  });

  it('外部 signal 中止：fetch 的 signal 同步中止，以 AbortError 拒绝（不归一化为 timeout）', async () => {
    const external = new AbortController();
    let fetchedSignal: AbortSignal | undefined;
    const fetchMock = vi.fn<typeof fetch>(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          fetchedSignal = init?.signal ?? undefined;
          init?.signal?.addEventListener('abort', () =>
            reject(new DOMException('The operation was aborted.', 'AbortError')),
          );
        }),
    );
    const client = createApiClient({
      getAccessToken: () => 'tok',
      refresh: vi.fn(),
      fetchFn: fetchMock,
      timeoutMs: 10_000,
    });
    const pending = client.request('/x', { signal: external.signal });
    external.abort();
    const error = await pending.catch((caught: unknown) => caught);
    // 外部与内部共用同一 controller 语义：传给 fetch 的 signal 必须兑现中止
    expect(fetchedSignal?.aborted).toBe(true);
    expect(error).toBeInstanceOf(DOMException);
    expect((error as DOMException).name).toBe('AbortError');
  });

  it('外部 signal 传入前已中止：不发请求，立即以 AbortError 拒绝', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => jsonResponse(200, {}));
    const client = createApiClient({ getAccessToken: () => 'tok', refresh: vi.fn(), fetchFn: fetchMock });
    const external = new AbortController();
    external.abort();
    const error = await client.request('/x', { signal: external.signal }).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(DOMException);
    expect((error as DOMException).name).toBe('AbortError');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('外部中止优先于 401：不 refresh/重试（fetch 未兑现 signal 时的兜底路径）', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => contractError(401, 'invalid_token'));
    const refresh = vi.fn(async () => 'tok_new');
    const client = createApiClient({ getAccessToken: () => 'tok', refresh, fetchFn: fetchMock });
    const external = new AbortController();
    const pending = client.request('/x', { signal: external.signal });
    external.abort();
    const error = await pending.catch((caught: unknown) => caught);
    expect((error as DOMException).name).toBe('AbortError');
    expect(refresh).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('外部 signal 未中止时不改变内部超时语义（仍归一化为 timeout）', async () => {
    const external = new AbortController();
    const fetchMock = vi.fn<typeof fetch>(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () =>
            reject(new DOMException('The operation was aborted.', 'AbortError')),
          );
        }),
    );
    const client = createApiClient({
      getAccessToken: () => null,
      refresh: vi.fn(),
      fetchFn: fetchMock,
      timeoutMs: 10,
    });
    const error = (await client
      .request('/x', { signal: external.signal })
      .catch((caught: unknown) => caught)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe('timeout');
  });

  it('204 响应解析为 undefined', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => new Response(null, { status: 204 }));
    const client = createApiClient({ getAccessToken: () => 'tok', refresh: vi.fn(), fetchFn: fetchMock });
    await expect(client.request<void>('/auth/logout', { method: 'POST' })).resolves.toBeUndefined();
  });

  it('sends FormData unchanged without forcing a JSON content type', async () => {
    const payload = new FormData();
    payload.set('file', new File(['avatar'], 'avatar.png', { type: 'image/png' }));
    const fetchMock = vi.fn<typeof fetch>(async () => jsonResponse(200, { avatar_url: '/avatar.png' }));
    const client = createApiClient({ getAccessToken: () => 'tok', refresh: vi.fn(), fetchFn: fetchMock });

    await client.request('/users/me/avatar', { method: 'POST', body: payload });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.body).toBe(payload);
    expect((init.headers as Record<string, string>)['Content-Type']).toBeUndefined();
    expect((init.headers as Record<string, string>)['Authorization']).toBe('Bearer tok');
  });

  it('Blob 成功响应保留二进制 bytes 和 MIME type', async () => {
    const expectedBytes = new Uint8Array([0, 1, 2, 255]);
    const fetchMock = vi.fn<typeof fetch>(async () =>
      new Response(expectedBytes, { status: 200, headers: { 'Content-Type': 'application/pdf' } }),
    );
    const client = createApiClient({ getAccessToken: () => 'tok', refresh: vi.fn(), fetchFn: fetchMock });

    const result = await client.request('/submissions/sub_1/content', { responseType: 'blob' });

    expect(Object.prototype.toString.call(result)).toBe('[object Blob]');
    expect(result.type).toBe('application/pdf');
    expect([...new Uint8Array(await result.arrayBuffer())]).toEqual([...expectedBytes]);
  });

  it('Blob 请求的 JSON 401 只 refresh/replay 一次，并返回重试后的 Blob', async () => {
    let token: string | null = 'tok_old';
    const expectedBytes = new Uint8Array([3, 1, 4, 1]);
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockImplementationOnce(async () => contractError(401, 'invalid_token'))
      .mockImplementationOnce(async () =>
        new Response(expectedBytes, { status: 200, headers: { 'Content-Type': 'application/pdf' } }),
      );
    const refresh = vi.fn(async () => {
      token = 'tok_new';
      return 'tok_new';
    });
    const client = createApiClient({ getAccessToken: () => token, refresh, fetchFn: fetchMock });

    const result = await client.request('/submissions/sub_1/content', { responseType: 'blob' });

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [, retryInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect((retryInit.headers as Record<string, string>)['Authorization']).toBe('Bearer tok_new');
    expect(result.type).toBe('application/pdf');
    expect([...new Uint8Array(await result.arrayBuffer())]).toEqual([...expectedBytes]);
  });

  it('Blob 成功响应在 headers 后正文解码卡住时仍会在 deadline 超时', async () => {
    vi.useFakeTimers();
    try {
      let signal: AbortSignal | undefined;
      const blob = vi.fn(() => new Promise<Blob>(() => {}));
      const fetchMock = vi.fn<typeof fetch>((_url, init) => {
        signal = init?.signal ?? undefined;
        return Promise.resolve({ ok: true, status: 200, blob } as unknown as Response);
      });
      const client = createApiClient({
        getAccessToken: () => 'tok',
        refresh: vi.fn(),
        fetchFn: fetchMock,
        timeoutMs: 10,
      });
      let outcome: unknown;
      void client.request('/submissions/sub_1/content', { responseType: 'blob' }).then(
        () => {
          outcome = 'resolved';
        },
        (error: unknown) => {
          outcome = error;
        },
      );

      await vi.advanceTimersByTimeAsync(0);
      expect(blob).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(10);

      expect(signal?.aborted).toBe(true);
      expect(outcome).toBeInstanceOf(ApiError);
      expect((outcome as ApiError).code).toBe('timeout');
    } finally {
      vi.useRealTimers();
    }
  });

  it('Blob 请求的 401 在 headers 后错误正文卡住时超时且不 refresh/replay', async () => {
    vi.useFakeTimers();
    try {
      let signal: AbortSignal | undefined;
      const json = vi.fn(() => new Promise<unknown>(() => {}));
      const fetchMock = vi.fn<typeof fetch>((_url, init) => {
        signal = init?.signal ?? undefined;
        return Promise.resolve({ ok: false, status: 401, json } as unknown as Response);
      });
      const refresh = vi.fn(async () => 'tok_new');
      const client = createApiClient({
        getAccessToken: () => 'tok_old',
        refresh,
        fetchFn: fetchMock,
        timeoutMs: 10,
      });
      let outcome: unknown;
      void client.request('/submissions/sub_1/content', { responseType: 'blob' }).then(
        () => {
          outcome = 'resolved';
        },
        (error: unknown) => {
          outcome = error;
        },
      );

      await vi.advanceTimersByTimeAsync(0);
      expect(json).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(10);

      expect(signal?.aborted).toBe(true);
      expect(outcome).toBeInstanceOf(ApiError);
      expect((outcome as ApiError).code).toBe('timeout');
      expect(refresh).not.toHaveBeenCalled();
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('Blob 正文 reader 拒绝时归一化为 ApiError', async () => {
    const decoderFailure = new TypeError('stream reader failed');
    const fetchMock = vi.fn<typeof fetch>(async () =>
      ({
        ok: true,
        status: 200,
        blob: vi.fn(async () => {
          throw decoderFailure;
        }),
      }) as unknown as Response,
    );
    const client = createApiClient({ getAccessToken: () => 'tok', refresh: vi.fn(), fetchFn: fetchMock });

    const error = await client
      .request('/submissions/sub_1/content', { responseType: 'blob' })
      .catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).not.toBe(decoderFailure);
    expect((error as ApiError).code).toBe('network_error');
  });

  it('guarded JSON 401 does not refresh or replay after the logical session changes', async () => {
    let token: string | null = 'tok_a';
    let authSessionId: string | null = 'logical_a';
    const fetchMock = vi.fn<typeof fetch>(async () => {
      token = 'tok_b';
      authSessionId = 'logical_b';
      return contractError(401, 'invalid_token');
    });
    const refresh = vi.fn(async () => 'tok_b');
    const client = createApiClient({
      getAccessToken: () => token,
      getAuthSessionId: () => authSessionId,
      refresh,
      fetchFn: fetchMock,
    });

    const authSessionGuard = client.captureAuthSessionGuard();
    await expect(
      client.request('/users/me/profile', {
        method: 'PATCH',
        body: { display_name: 'from A' },
        authSessionGuard,
      }),
    ).rejects.toMatchObject({ code: 'stale_auth_session', status: null });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(refresh).not.toHaveBeenCalled();
  });

  it('guarded successful response is discarded after the logical session changes while it is in flight', async () => {
    let authSessionId: string | null = 'logical_a';
    let resolveResponse: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn<typeof fetch>(
      () =>
        new Promise<Response>((resolve) => {
          resolveResponse = resolve;
        }),
    );
    const refresh = vi.fn(async () => 'tok_b');
    const client = createApiClient({
      getAccessToken: () => 'tok_a',
      getAuthSessionId: () => authSessionId,
      refresh,
      fetchFn: fetchMock,
    });

    const authSessionGuard = client.captureAuthSessionGuard();
    const pending = client.request('/users/me/preferences', { authSessionGuard });
    authSessionId = 'logical_b';
    resolveResponse?.(jsonResponse(200, { theme: 'dark' }));

    await expect(pending).rejects.toMatchObject({ code: 'stale_auth_session', status: null });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(refresh).not.toHaveBeenCalled();
  });

  it('guarded FormData 401 does not replay after refresh changes the logical session', async () => {
    let token: string | null = 'tok_a';
    let authSessionId: string | null = 'logical_a';
    const payload = new FormData();
    payload.set('file', new File(['avatar'], 'avatar.png', { type: 'image/png' }));
    const fetchMock = vi.fn<typeof fetch>(async () => contractError(401, 'invalid_token'));
    const refresh = vi.fn(async () => {
      token = 'tok_b';
      authSessionId = 'logical_b';
      return 'tok_b';
    });
    const client = createApiClient({
      getAccessToken: () => token,
      getAuthSessionId: () => authSessionId,
      refresh,
      fetchFn: fetchMock,
    });

    const authSessionGuard = client.captureAuthSessionGuard();
    await expect(
      client.request('/users/me/avatar', {
        method: 'POST',
        body: payload,
        authSessionGuard,
      }),
    ).rejects.toMatchObject({ code: 'stale_auth_session', status: null });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(refresh).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.body).toBe(payload);
    expect((init.headers as Record<string, string>)['Content-Type']).toBeUndefined();
    expect((init.headers as Record<string, string>)['Authorization']).toBe('Bearer tok_a');
  });

  it('guarded same logical session still refreshes once and replays with the new bearer', async () => {
    let token: string | null = 'tok_old';
    let authSessionId: string | null = 'logical_same';
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockImplementationOnce(async () => contractError(401, 'invalid_token'))
      .mockImplementationOnce(async () => jsonResponse(200, { ok: true }));
    const refresh = vi.fn(async () => {
      token = 'tok_new';
      return 'tok_new';
    });
    const client = createApiClient({
      getAccessToken: () => token,
      getAuthSessionId: () => authSessionId,
      refresh,
      fetchFn: fetchMock,
    });

    const authSessionGuard = client.captureAuthSessionGuard();
    const result = await client.request<{ ok: boolean }>('/conversations', { authSessionGuard });
    expect(result.ok).toBe(true);
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [, secondInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect((secondInit.headers as Record<string, string>)['Authorization']).toBe('Bearer tok_new');
  });
});
