import type { APIRequestContext, Page } from '@playwright/test';

export const E2E_ADMIN_USER =
  process.env.E2E_ADMIN_USER || process.env.AUTH_LOCAL_ADMIN_USER || 'admin';
export const E2E_ADMIN_PASS =
  process.env.E2E_ADMIN_PASS || process.env.AUTH_LOCAL_ADMIN_PASS || '';

/** 从 AUTH_LOCAL_ADMIN_SEED=user:pass 解析（若未拆分 env） */
export function resolveAdminCredentials(): { username: string; password: string } {
  const seed = process.env.AUTH_LOCAL_ADMIN_SEED || '';
  if (seed.includes(':')) {
    const idx = seed.indexOf(':');
    return { username: seed.slice(0, idx), password: seed.slice(idx + 1) };
  }
  if (!E2E_ADMIN_PASS) {
    throw new Error(
      'Set AUTH_LOCAL_ADMIN_SEED or E2E_ADMIN_USER/E2E_ADMIN_PASS for e2e login',
    );
  }
  return { username: E2E_ADMIN_USER, password: E2E_ADMIN_PASS };
}

export function hasAdminCredentials(): boolean {
  try {
    resolveAdminCredentials();
    return true;
  } catch {
    return false;
  }
}

export async function apiLogin(
  request: APIRequestContext,
  username: string,
  password: string,
): Promise<void> {
  const res = await request.post('/api/auth/login', {
    data: { username, password },
  });
  if (!res.ok()) {
    throw new Error(`login failed: ${res.status()} ${await res.text()}`);
  }
}

export async function loginWithUI(
  page: Page,
  username: string,
  password: string,
): Promise<void> {
  await page.goto('/login');
  await page.fill('input[name="username"]', username);
  await page.fill('input[name="password"]', password);
  await page.click('button[type="submit"]');
  await page.waitForURL('**/chat');
}

/** 使用 page.request 登录后 cookie 与 page 共享（同 baseURL 时） */
export async function loginViaApi(
  page: Page,
  username: string,
  password: string,
): Promise<void> {
  const res = await page.request.post('/api/auth/login', {
    data: { username, password },
  });
  if (!res.ok()) {
    throw new Error(`login failed: ${res.status()} ${await res.text()}`);
  }
}

/** admin 创建临时 viewer 用户；若后端无权限或 409 则抛错由用例处理 */
export async function createViewerUser(
  page: Page,
  username: string,
  password: string,
): Promise<void> {
  const res = await page.request.post('/api/admin/users', {
    data: {
      username,
      password,
      roles: ['viewer'],
      spaces: ['default'],
    },
  });
  if (!res.ok() && res.status() !== 409) {
    throw new Error(`create viewer failed: ${res.status()} ${await res.text()}`);
  }
}
