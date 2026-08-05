/*
 * CSRF Cookie 读取（规格 §3；契约 §2.10）。
 * refresh 请求不携带 Bearer、无 body，从 CSRF Cookie 读值经 X-CSRF-Token 原样回传。
 * refresh token 只存 HttpOnly Cookie，前端不可读；本模块只读取前端可读的 CSRF Cookie。
 */

/** 前端可读的 CSRF Cookie 名（mock 与真实后端约定值）。 */
export const CSRF_COOKIE_NAME = 'csrf_token';

export function readCookie(name: string, cookieString: string | undefined = undefined): string | null {
  const source = cookieString ?? globalThis.document?.cookie;
  if (typeof source !== 'string' || source.length === 0) {
    return null;
  }
  for (const entry of source.split(';')) {
    const [rawKey, ...rest] = entry.trim().split('=');
    if (rawKey === name) {
      return decodeURIComponent(rest.join('='));
    }
  }
  return null;
}

/** 读取 CSRF Cookie 值；不存在时返回 null（refresh 将由后端以 csrf_failed 拒绝）。 */
export function readCsrfToken(): string | null {
  return readCookie(CSRF_COOKIE_NAME);
}
