/*
 * API 客户端基座（规格 §2）。
 * - 所有请求带 /v1 前缀；除登录与 refresh 外携带 Authorization: Bearer <token>。
 * - 错误归一化为 ApiError（见 ./errors）。
 * - 业务请求 401：自动执行一次 refresh（single-flight 由会话层保证，并发请求等待同一次结果）
 *   后重试原请求一次；refresh 失败按认证失效处理（会话层清理状态），错误继续上抛。
 */

import { ApiError, networkError, normalizeApiError, timeoutError } from './errors';

export interface ApiRequestOptions {
  readonly method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  readonly body?: unknown;
  /** false 时不携带 Bearer，也不触发 401 自动 refresh（登录与 refresh 自身）。 */
  readonly auth?: boolean;
  readonly headers?: Record<string, string>;
  readonly timeoutMs?: number;
}

export interface ApiClientDeps {
  /** 默认 '/v1'。 */
  readonly baseUrl?: string;
  readonly getAccessToken: () => string | null;
  /** 业务请求 401 时调用；须为 single-flight 实现，resolve 新 access token。 */
  readonly refresh: () => Promise<string>;
  readonly fetchFn?: typeof fetch;
  /** 默认 10s。 */
  readonly timeoutMs?: number;
}

export interface ApiClient {
  request<T>(path: string, options?: ApiRequestOptions): Promise<T>;
}

const DEFAULT_TIMEOUT_MS = 10_000;

/** 相对路径解析为绝对 URL；浏览器与 jsdom/undici（要求绝对 URL）均可工作。 */
export function resolveUrl(path: string): string {
  const base = globalThis.location?.origin ?? 'http://localhost';
  return new URL(path, base).toString();
}

export function createApiClient(deps: ApiClientDeps): ApiClient {
  const baseUrl = deps.baseUrl ?? '/v1';
  const fetchFn = deps.fetchFn ?? ((...args: Parameters<typeof fetch>) => fetch(...args));

  async function rawRequest<T>(path: string, options: ApiRequestOptions): Promise<T> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options.timeoutMs ?? deps.timeoutMs ?? DEFAULT_TIMEOUT_MS);
    const headers: Record<string, string> = { ...options.headers };
    if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json';
    }
    if (options.auth !== false) {
      const token = deps.getAccessToken();
      if (token !== null) {
        headers['Authorization'] = `Bearer ${token}`;
      }
    }
    let response: Response;
    try {
      response = await fetchFn(resolveUrl(`${baseUrl}${path}`), {
        method: options.method ?? 'GET',
        headers,
        body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
        credentials: 'include',
        signal: controller.signal,
      });
    } catch (cause) {
      if (controller.signal.aborted) {
        throw timeoutError();
      }
      throw networkError(cause);
    } finally {
      clearTimeout(timeout);
    }
    if (response.status === 204) {
      return undefined as T;
    }
    const body: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      throw normalizeApiError(body, response.status);
    }
    return body as T;
  }

  return {
    async request<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
      try {
        return await rawRequest<T>(path, options);
      } catch (error) {
        const isUnauthorized = error instanceof ApiError && error.status === 401;
        if (options.auth === false || !isUnauthorized) {
          throw error;
        }
        // 业务请求 401：先 single-flight refresh 一次，再重试原请求；refresh 失败按认证失效上抛
        await deps.refresh();
        return await rawRequest<T>(path, options);
      }
    },
  };
}
