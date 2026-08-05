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

  it('204 响应解析为 undefined', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => new Response(null, { status: 204 }));
    const client = createApiClient({ getAccessToken: () => 'tok', refresh: vi.fn(), fetchFn: fetchMock });
    await expect(client.request<void>('/auth/logout', { method: 'POST' })).resolves.toBeUndefined();
  });
});
