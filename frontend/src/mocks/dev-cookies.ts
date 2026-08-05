/*
 * mock 的 Cookie 模拟（规格 §1）。
 * 真实后端的 refresh Cookie 是 host-only + HttpOnly，MSW 无法在浏览器中设置 HttpOnly Cookie，
 * 开发环境以普通 Cookie 模拟（仅 mock 层读写，业务代码永不读取 refresh Cookie）；
 * CSRF Cookie 本就是前端可读 Cookie，行为与生产一致。
 * 测试环境（jsdom）同样经 document.cookie 读写；无 DOM 时退化为内存 jar。
 */

import { CSRF_COOKIE_NAME } from '../auth/cookies';

export const REFRESH_COOKIE_NAME = 'refresh_token';

const memoryJar = new Map<string, string>();

function hasDomCookie(): boolean {
  return typeof document !== 'undefined' && typeof document.cookie === 'string';
}

export function getMockCookie(name: string): string | null {
  if (!hasDomCookie()) {
    return memoryJar.get(name) ?? null;
  }
  for (const entry of document.cookie.split(';')) {
    const [rawKey, ...rest] = entry.trim().split('=');
    if (rawKey === name) {
      return decodeURIComponent(rest.join('='));
    }
  }
  return null;
}

export function setMockCookie(name: string, value: string): void {
  if (!hasDomCookie()) {
    memoryJar.set(name, value);
    return;
  }
  document.cookie = `${name}=${encodeURIComponent(value)}; path=/; SameSite=Lax`;
}

export function clearMockCookie(name: string): void {
  if (!hasDomCookie()) {
    memoryJar.delete(name);
    return;
  }
  document.cookie = `${name}=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT`;
}

/** 登出、设备撤销与认证失效时清除两个 Cookie（mock 服务端职责，契约 §2.1/§2.2/§2.8/§2.10）。 */
export function clearAuthCookies(): void {
  clearMockCookie(REFRESH_COOKIE_NAME);
  clearMockCookie(CSRF_COOKIE_NAME);
}
