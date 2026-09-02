import { describe, expect, it, vi } from 'vitest';
import type { ApiClient, AuthSessionGuard, JsonApiRequestOptions } from '../api/client';
import { createApiClient } from '../api/client';
import type { QuotaSnapshot, UserPreferences } from './types';
import { createSettingsApi } from './api';

/** JSON overload of ApiClient.request — avoids vi.fn collapsing to the Blob overload. */
type ApiRequestDouble = (path: string, options?: JsonApiRequestOptions) => Promise<unknown>;

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function captureFetch(mock: ReturnType<typeof vi.fn<typeof fetch>>): { url: string; init: RequestInit } {
  const [url, init] = mock.mock.calls.at(-1) as [string, RequestInit];
  return { url, init };
}

function makeApi(mock: ReturnType<typeof vi.fn<typeof fetch>>) {
  const client = createApiClient({
    getAccessToken: () => 'tok_1',
    refresh: vi.fn(),
    fetchFn: mock,
  });
  return createSettingsApi(client);
}

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

const sampleUser = {
  id: 'u_1',
  username: 'zhangsan',
  display_name: '张三',
  real_name: '张三',
  department: { id: 'd_1', name: '财务部' },
  role: 'user' as const,
  avatar_url: null,
};

const samplePreferences: UserPreferences = {
  theme: 'system',
  chat_font_size: 'standard',
  ab_opt_out: false,
};

const sampleQuota: QuotaSnapshot = {
  used: 120,
  base_limit: 500,
  extra_granted: 0,
  effective_limit: 500,
  unlimited: false,
  reset_at: '2026-09-01T00:00:00+08:00',
  business_timezone: 'Asia/Shanghai',
  quota_period: '2026-08',
  business_calendar_version_id: 'calendar_1',
  pending_request: null,
};

describe('设置域 API 封装（账户基座）', () => {
  it('PATCH /users/me/profile：透传 display_name 并返回 UserProfile', async () => {
    const mock = vi.fn<typeof fetch>(async () =>
      jsonResponse(200, { ...sampleUser, display_name: '新名字' }),
    );
    const api = makeApi(mock);
    const result = await api.updateProfile({ display_name: '新名字' });
    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/users/me/profile');
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(String(init.body))).toEqual({ display_name: '新名字' });
    expect((init.headers as Record<string, string>)['Authorization']).toBe('Bearer tok_1');
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json');
    expect(result.display_name).toBe('新名字');
  });

  it('POST /users/me/avatar：FormData 字段 file，不强制 Content-Type', async () => {
    const mock = vi.fn<typeof fetch>(async () => jsonResponse(200, { avatar_url: '/avatars/u_1.png' }));
    const api = makeApi(mock);
    const file = new File(['avatar'], 'avatar.png', { type: 'image/png' });
    const result = await api.uploadAvatar(file);
    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/users/me/avatar');
    expect(init.method).toBe('POST');
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).get('file')).toBe(file);
    expect((init.headers as Record<string, string>)['Content-Type']).toBeUndefined();
    expect((init.headers as Record<string, string>)['Authorization']).toBe('Bearer tok_1');
    expect(result.avatar_url).toBe('/avatars/u_1.png');
  });

  it('PUT /users/me/password：旧/新密码 body，204 解析为 undefined', async () => {
    const mock = vi.fn<typeof fetch>(async () => new Response(null, { status: 204 }));
    const api = makeApi(mock);
    await expect(
      api.changePassword({ old_password: 'oldpass1', new_password: 'newpass2' }),
    ).resolves.toBeUndefined();
    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/users/me/password');
    expect(init.method).toBe('PUT');
    expect(JSON.parse(String(init.body))).toEqual({
      old_password: 'oldpass1',
      new_password: 'newpass2',
    });
  });

  it('GET /users/me/preferences：无 body，返回偏好结构', async () => {
    const mock = vi.fn<typeof fetch>(async () => jsonResponse(200, samplePreferences));
    const api = makeApi(mock);
    const result = await api.getPreferences();
    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/users/me/preferences');
    expect(init.method).toBe('GET');
    expect(init.body).toBeUndefined();
    expect(result).toEqual(samplePreferences);
  });

  it('PUT /users/me/preferences：整对象替换并回传', async () => {
    const next: UserPreferences = { theme: 'dark', chat_font_size: 'large', ab_opt_out: true };
    const mock = vi.fn<typeof fetch>(async () => jsonResponse(200, next));
    const api = makeApi(mock);
    const result = await api.updatePreferences(next);
    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/users/me/preferences');
    expect(init.method).toBe('PUT');
    expect(JSON.parse(String(init.body))).toEqual(next);
    expect(result).toEqual(next);
  });

  it('GET /quota/me：无 body，返回服务端 quota snapshot', async () => {
    const mock = vi.fn<typeof fetch>(async () => jsonResponse(200, sampleQuota));
    const result = await makeApi(mock).getQuota();
    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/quota/me');
    expect(init.method).toBe('GET');
    expect(init.body).toBeUndefined();
    expect(result).toEqual(sampleQuota);
  });

  it('写操作 requestQuota 携带 Idempotency-Key 与 requested_pages', async () => {
    const mock = vi.fn<typeof fetch>(async () =>
      jsonResponse(201, {
        id: 'qr_1',
        version: 1,
        status: 'pending',
        requested_pages: 100,
        quota_period: '2026-08',
        created_at: '2026-08-01T00:00:00Z',
      }),
    );
    const api = makeApi(mock);
    await api.requestQuota(100, 'idem-quota-1');
    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/quota-requests');
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ requested_pages: 100 });
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toBe('idem-quota-1');
  });

  it('cancelJob：无请求体且不携带 Idempotency-Key', async () => {
    const mock = vi.fn<typeof fetch>(async () => new Response(null, { status: 204 }));
    const api = makeApi(mock);

    await api.cancelJob('job id/1');

    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/ingestion-jobs/job%20id%2F1/cancel');
    expect(init.method).toBe('POST');
    expect(init.body).toBeUndefined();
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toBeUndefined();
  });

  it('uploadDocuments：字节级 multipart（真实文件名）多文件 + Idempotency-Key + multipart Content-Type', async () => {
    const mock = vi.fn<typeof fetch>(async () =>
      jsonResponse(200, { upload_batch_id: 'ub_1', items: [] }),
    );
    const api = makeApi(mock);
    const files = [
      new File(['a'], 'a.pdf', { type: 'application/pdf' }),
      new File(['b'], 'b.pdf', { type: 'application/pdf' }),
    ] as const;
    await api.uploadDocuments('personal:u_1', files, 'idem-upload-1');
    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/spaces/personal%3Au_1/documents');
    expect(init.method).toBe('POST');
    expect(init.body).toBeInstanceOf(Uint8Array);
    // 字节体含真实文件名（undici 对 FormData 序列化会抹成 'blob'，字节级手工 multipart 保证文件名）
    const text = new TextDecoder('latin1').decode(init.body as Uint8Array);
    expect(text).toContain('filename="a.pdf"');
    expect(text).toContain('filename="b.pdf"');
    expect((init.headers as Record<string, string>)['Content-Type']).toMatch(
      /^multipart\/form-data; boundary=----RAGqsBoundary/,
    );
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toBe('idem-upload-1');
  });

  it('uploadNewVersion：使用后端要求的单数 file multipart 字段', async () => {
    const mock = vi.fn<typeof fetch>(async () =>
      jsonResponse(202, {
        document_id: 'doc_1',
        document_version_id: 'version_2',
        job_id: 'job_2',
        publication_id: 'publication_2',
        version: 2,
        deduplicated: false,
        status: 'pending',
      }),
    );
    const api = makeApi(mock);

    const result = await api.uploadNewVersion(
      'doc_1',
      new File(['replacement'], 'replacement.pdf', { type: 'application/pdf' }),
      1,
      'idem-version-1',
    );

    const { init } = captureFetch(mock);
    const text = new TextDecoder('latin1').decode(init.body as Uint8Array);
    expect(text).toContain('name="file"; filename="replacement.pdf"');
    expect(text).not.toContain('name="files";');
    expect(text).toContain('name="expected_version"');
    expect(result).toMatchObject({
      document_id: 'doc_1',
      document_version_id: 'version_2',
      job_id: 'job_2',
      publication_id: 'publication_2',
      deduplicated: false,
      status: 'pending',
    });
  });

  it('getSubmissionContent：请求 Blob 内容并保留 bytes 和 MIME type', async () => {
    const expectedBytes = new Uint8Array([37, 80, 68, 70]);
    const mock = vi.fn<typeof fetch>(async () =>
      new Response(expectedBytes, { status: 200, headers: { 'Content-Type': 'application/pdf' } }),
    );
    const api = makeApi(mock);

    const result = await api.getSubmissionContent('sub id/1');

    const { url, init } = captureFetch(mock);
    expect(url).toContain('/v1/submissions/sub%20id%2F1/content');
    expect(init.method).toBe('GET');
    expect(Object.prototype.toString.call(result)).toBe('[object Blob]');
    expect(result.type).toBe('application/pdf');
    expect([...new Uint8Array(await result.arrayBuffer())]).toEqual([...expectedBytes]);
  });
});

describe('大文件传输显式超时（A17）', () => {
  it('uploadDocuments / uploadNewVersion / getSubmissionContent：timeoutMs 显式 120s（不走基座默认 10s）', async () => {
    const { client, request } = createClientDouble();
    const api = createSettingsApi(client);

    await api.uploadDocuments('personal:u_1', [new File(['a'], 'a.pdf', { type: 'application/pdf' })], 'idem-t-1');
    await api.uploadNewVersion('doc_1', new File(['b'], 'b.pdf', { type: 'application/pdf' }), 1, 'idem-t-2');
    await api.getSubmissionContent('sub_1');

    expect(request.mock.calls[0]?.[1]).toMatchObject({ timeoutMs: 120_000 });
    expect(request.mock.calls[1]?.[1]).toMatchObject({ timeoutMs: 120_000 });
    expect(request.mock.calls[2]?.[1]).toMatchObject({ timeoutMs: 120_000 });
  });
});

describe('设置域 API 敏感 mutation 的 authSessionGuard', () => {
  it('createSettingsApi 工厂创建时不调用 captureAuthSessionGuard', () => {
    const { client, captureAuthSessionGuard } = createClientDouble();

    createSettingsApi(client);

    expect(captureAuthSessionGuard).not.toHaveBeenCalled();
  });

  it('cancelJob：只传 POST 与 authSessionGuard，不构造幂等请求头', async () => {
    const { client, captureAuthSessionGuard, request, authSessionGuard } = createClientDouble();
    const api = createSettingsApi(client);

    await api.cancelJob('job_1');

    expect(captureAuthSessionGuard).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith('/ingestion-jobs/job_1/cancel', {
      method: 'POST',
      authSessionGuard,
    });
  });

  it('updateProfile：保留 method/body，authSessionGuard 与 capture 返回值同一引用，且 capture 先于 request', async () => {
    const { client, captureAuthSessionGuard, request, authSessionGuard } = createClientDouble();
    request.mockResolvedValueOnce({ display_name: '新名字' });
    const api = createSettingsApi(client);

    await api.updateProfile({ display_name: '新名字' });

    expect(captureAuthSessionGuard).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith('/users/me/profile', {
      method: 'PATCH',
      body: { display_name: '新名字' },
      authSessionGuard,
    });
    expect((request.mock.calls[0]?.[1] as { authSessionGuard: AuthSessionGuard }).authSessionGuard).toBe(
      authSessionGuard,
    );
    expect(captureAuthSessionGuard.mock.invocationCallOrder[0]!).toBeLessThan(
      request.mock.invocationCallOrder[0]!,
    );
  });

  it('uploadAvatar：保留 FormData body，authSessionGuard 与 capture 返回值同一引用', async () => {
    const { client, captureAuthSessionGuard, request, authSessionGuard } = createClientDouble();
    request.mockResolvedValueOnce({ avatar_url: '/avatars/u_1.png' });
    const api = createSettingsApi(client);
    const file = new File(['avatar'], 'avatar.png', { type: 'image/png' });

    await api.uploadAvatar(file);

    expect(captureAuthSessionGuard).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledTimes(1);
    const [path, options] = request.mock.calls[0] as [
      string,
      { method: string; body: FormData; authSessionGuard: AuthSessionGuard },
    ];
    expect(path).toBe('/users/me/avatar');
    expect(options.method).toBe('POST');
    expect(options.body).toBeInstanceOf(FormData);
    expect(options.body.get('file')).toBe(file);
    expect(options.authSessionGuard).toBe(authSessionGuard);
    expect(captureAuthSessionGuard.mock.invocationCallOrder[0]!).toBeLessThan(
      request.mock.invocationCallOrder[0]!,
    );
  });

  it('changePassword：保留 method/body，authSessionGuard 与 capture 返回值同一引用', async () => {
    const { client, captureAuthSessionGuard, request, authSessionGuard } = createClientDouble();
    const api = createSettingsApi(client);

    await api.changePassword({ old_password: 'oldpass1', new_password: 'newpass2' });

    expect(captureAuthSessionGuard).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith('/users/me/password', {
      method: 'PUT',
      body: { old_password: 'oldpass1', new_password: 'newpass2' },
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
    const request = vi.fn<ApiRequestDouble>(async () => ({ display_name: 'x' }));
    const client = { captureAuthSessionGuard, request } as unknown as ApiClient;
    const api = createSettingsApi(client);

    await api.updateProfile({ display_name: '一次' });
    await api.updateProfile({ display_name: '两次' });

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

  it('getPreferences：capture guard 后原样传入 request，且 capture 先于 request', async () => {
    const { client, captureAuthSessionGuard, request, authSessionGuard } = createClientDouble();
    request.mockResolvedValueOnce(samplePreferences);
    const api = createSettingsApi(client);

    await api.getPreferences();

    expect(captureAuthSessionGuard).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith('/users/me/preferences', { authSessionGuard });
    expect(captureAuthSessionGuard.mock.invocationCallOrder[0]!).toBeLessThan(
      request.mock.invocationCallOrder[0]!,
    );
  });

  it('updatePreferences：每次完整 snapshot mutation 都 capture 并绑定当前 guard', async () => {
    const guard1: AuthSessionGuard = { authSessionId: 'logical_a' };
    const guard2: AuthSessionGuard = { authSessionId: 'logical_b' };
    const captureAuthSessionGuard = vi
      .fn<() => AuthSessionGuard>()
      .mockReturnValueOnce(guard1)
      .mockReturnValueOnce(guard2);
    const request = vi.fn<ApiRequestDouble>(async (_path, options) => options?.body);
    const client = { captureAuthSessionGuard, request } as unknown as ApiClient;
    const api = createSettingsApi(client);
    const first: UserPreferences = { theme: 'dark', chat_font_size: 'standard', ab_opt_out: false };
    const second: UserPreferences = { theme: 'light', chat_font_size: 'large', ab_opt_out: true };

    await api.updatePreferences(first);
    await api.updatePreferences(second);

    expect(captureAuthSessionGuard).toHaveBeenCalledTimes(2);
    expect(request).toHaveBeenCalledTimes(2);
    expect(request.mock.calls[0]).toEqual([
      '/users/me/preferences',
      { method: 'PUT', body: first, authSessionGuard: guard1 },
    ]);
    expect(request.mock.calls[1]).toEqual([
      '/users/me/preferences',
      { method: 'PUT', body: second, authSessionGuard: guard2 },
    ]);
    expect(captureAuthSessionGuard.mock.invocationCallOrder[0]!).toBeLessThan(
      request.mock.invocationCallOrder[0]!,
    );
    expect(captureAuthSessionGuard.mock.invocationCallOrder[1]!).toBeLessThan(
      request.mock.invocationCallOrder[1]!,
    );
  });
});
