/*
 * API 客户端基座（规格 §2）。
 * - 所有请求带 /v1 前缀；除登录与 refresh 外携带 Authorization: Bearer <token>。
 * - 错误归一化为 ApiError（见 ./errors）。
 * - 业务请求 401：自动执行一次 refresh（single-flight 由会话层保证，并发请求等待同一次结果）
 *   后重试原请求一次；refresh 失败按认证失效处理（会话层清理状态），错误继续上抛。
 */

import { ApiError, networkError, normalizeApiError, staleAuthSessionError, timeoutError } from './errors';

export interface AuthSessionGuard {
  readonly authSessionId: string | null;
}

export interface ApiRequestOptions {
  readonly method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  readonly body?: unknown;
  /** false 时不携带 Bearer，也不触发 401 自动 refresh（登录与 refresh 自身）。 */
  readonly auth?: boolean;
  readonly headers?: Record<string, string>;
  readonly timeoutMs?: number;
  /** 成功响应解码方式；默认 JSON，二进制内容端点显式选择 blob。 */
  readonly responseType?: 'json' | 'blob';
  /** 可选：捕获时的逻辑会话；与当前会话不一致时 fail-closed，不 refresh/replay。 */
  readonly authSessionGuard?: AuthSessionGuard;
}

export interface ApiClientDeps {
  /** 默认 '/v1'。 */
  readonly baseUrl?: string;
  readonly getAccessToken: () => string | null;
  /** 当前逻辑认证会话 id；缺省时 guard 视作始终 null。 */
  readonly getAuthSessionId?: () => string | null;
  /** 业务请求 401 时调用；须为 single-flight 实现，resolve 新 access token。 */
  readonly refresh: () => Promise<string>;
  readonly fetchFn?: typeof fetch;
  /** 默认 10s。 */
  readonly timeoutMs?: number;
}

export interface JsonApiRequestOptions extends ApiRequestOptions {
  readonly responseType?: 'json';
}

export interface BlobApiRequestOptions extends ApiRequestOptions {
  readonly responseType: 'blob';
}

export interface ApiClient {
  captureAuthSessionGuard(): AuthSessionGuard;
  request<T>(path: string, options?: JsonApiRequestOptions): Promise<T>;
  request(path: string, options: BlobApiRequestOptions): Promise<Blob>;
}

const DEFAULT_TIMEOUT_MS = 10_000;

/** 相对路径解析为绝对 URL；浏览器与 jsdom/undici（要求绝对 URL）均可工作。 */
export function resolveUrl(path: string): string {
  const base = globalThis.location?.origin ?? 'http://localhost';
  return new URL(path, base).toString();
}

function readBodyWithDeadline<T>(read: () => Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) {
    return Promise.reject(timeoutError());
  }
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => {
      signal.removeEventListener('abort', onAbort);
      reject(timeoutError());
    };
    signal.addEventListener('abort', onAbort, { once: true });
    Promise.resolve()
      .then(read)
      .then(
        (body) => {
          signal.removeEventListener('abort', onAbort);
          resolve(body);
        },
        (cause) => {
          signal.removeEventListener('abort', onAbort);
          reject(cause);
        },
      );
  });
}

export function createApiClient(deps: ApiClientDeps): ApiClient {
  const baseUrl = deps.baseUrl ?? '/v1';
  const fetchFn = deps.fetchFn ?? ((...args: Parameters<typeof fetch>) => fetch(...args));
  const currentAuthSessionId = (): string | null => deps.getAuthSessionId?.() ?? null;

  function assertAuthSessionGuard(guard: AuthSessionGuard | undefined): void {
    if (guard !== undefined && guard.authSessionId !== currentAuthSessionId()) {
      throw staleAuthSessionError();
    }
  }

  async function rawRequest<T>(path: string, options: ApiRequestOptions): Promise<T> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options.timeoutMs ?? deps.timeoutMs ?? DEFAULT_TIMEOUT_MS);
    const headers: Record<string, string> = { ...options.headers };
    // multipart 请求体不强制 JSON Content-Type：
    // - FormData / multipart Blob：边界由 body 自带；
    // - 字节级手工 multipart：调用方已在 headers 显式给出 multipart Content-Type（含边界）。
    const multipartHeader =
      typeof headers['Content-Type'] === 'string' && headers['Content-Type'].startsWith('multipart/form-data');
    const isMultipartBody =
      options.body instanceof FormData ||
      (typeof Blob !== 'undefined' && options.body instanceof Blob && options.body.type.startsWith('multipart/form-data')) ||
      multipartHeader;
    if (options.body !== undefined && !isMultipartBody) {
      headers['Content-Type'] = 'application/json';
    }
    if (options.auth !== false) {
      const token = deps.getAccessToken();
      if (token !== null) {
        headers['Authorization'] = `Bearer ${token}`;
      }
    }
    const requestBody =
      options.body === undefined ? undefined : isMultipartBody ? options.body : JSON.stringify(options.body);
    try {
      const response = await fetchFn(resolveUrl(`${baseUrl}${path}`), {
        method: options.method ?? 'GET',
        headers,
        body: requestBody as BodyInit | undefined,
        credentials: 'include',
        signal: controller.signal,
      });
      if (response.status === 204) {
        return undefined as T;
      }
      // 错误响应始终读取 JSON 契约；仅成功二进制内容改走 Blob 解码。
      const body: unknown =
        response.ok && options.responseType === 'blob'
          ? await readBodyWithDeadline(() => response.blob(), controller.signal)
          : await readBodyWithDeadline(() => response.json(), controller.signal).catch((cause) => {
              if (cause instanceof ApiError) {
                throw cause;
              }
              if (controller.signal.aborted) {
                throw timeoutError();
              }
              if (response.ok) {
                throw normalizeApiError(null, response.status);
              }
              return null;
            });
      if (!response.ok) {
        throw normalizeApiError(body, response.status);
      }
      return body as T;
    } catch (cause) {
      if (cause instanceof ApiError) {
        throw cause;
      }
      if (controller.signal.aborted) {
        throw timeoutError();
      }
      throw networkError(cause);
    } finally {
      clearTimeout(timeout);
    }
  }

  async function request<T>(path: string, options?: JsonApiRequestOptions): Promise<T>;
  async function request(path: string, options: BlobApiRequestOptions): Promise<Blob>;
  async function request(path: string, options: ApiRequestOptions = {}): Promise<unknown> {
    assertAuthSessionGuard(options.authSessionGuard);
    try {
      const result = await rawRequest<unknown>(path, options);
      // Guarded responses must fail closed even when the original request succeeded after a session switch.
      assertAuthSessionGuard(options.authSessionGuard);
      return result;
    } catch (error) {
      const isUnauthorized = error instanceof ApiError && error.status === 401;
      if (options.auth === false || !isUnauthorized) {
        throw error;
      }
      // 业务请求 401：先 single-flight refresh 一次，再重试原请求；refresh 失败按认证失效上抛
      assertAuthSessionGuard(options.authSessionGuard);
      await deps.refresh();
      assertAuthSessionGuard(options.authSessionGuard);
      const result = await rawRequest<unknown>(path, options);
      assertAuthSessionGuard(options.authSessionGuard);
      return result;
    }
  }

  return {
    captureAuthSessionGuard: () => ({ authSessionId: currentAuthSessionId() }),
    request,
  };
}
