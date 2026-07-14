import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiJson, ApiError, registerUnauthorizedHandler } from './client';

describe('apiJson unauthorized handler', () => {
  beforeEach(() => {
    registerUnauthorizedHandler(null);
  });

  afterEach(() => {
    registerUnauthorizedHandler(null);
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('apiJson 401 invokes registered unauthorized handler then throws', async () => {
    const handler = vi.fn();
    registerUnauthorizedHandler(handler);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 401,
        statusText: 'Unauthorized',
        json: async () => ({ detail: 'unauthorized' }),
      }),
    );

    await expect(apiJson('/knowledge-spaces')).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('apiJson non-401 errors do not invoke unauthorized handler', async () => {
    const handler = vi.fn();
    registerUnauthorizedHandler(handler);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 403,
        statusText: 'Forbidden',
        json: async () => ({ detail: 'forbidden' }),
      }),
    );

    await expect(apiJson('/admin/users')).rejects.toBeInstanceOf(ApiError);
    expect(handler).not.toHaveBeenCalled();
  });

  it('apiJson 401 still throws ApiError when no handler registered', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 401,
        statusText: 'Unauthorized',
        json: async () => ({ detail: 'unauthorized' }),
      }),
    );

    await expect(apiJson('/auth/me')).rejects.toMatchObject({
      name: 'ApiError',
      status: 401,
    });
  });

  it('apiJson 401 with skipUnauthorizedHandler does not invoke handler', async () => {
    const handler = vi.fn();
    registerUnauthorizedHandler(handler);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 401,
        statusText: 'Unauthorized',
        json: async () => ({ detail: 'unauthorized' }),
      }),
    );

    await expect(
      apiJson('/auth/me', undefined, { skipUnauthorizedHandler: true }),
    ).rejects.toMatchObject({
      name: 'ApiError',
      status: 401,
    });
    expect(handler).not.toHaveBeenCalled();
  });
});
