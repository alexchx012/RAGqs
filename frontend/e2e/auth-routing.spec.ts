import { test, expect } from '@playwright/test';
import {
  hasAdminCredentials,
  resolveAdminCredentials,
  loginWithUI,
  loginViaApi,
  createViewerUser,
} from './fixtures/auth';

/** 模拟未登录：/auth/me 返回 401（不依赖后端是否在线） */
async function mockUnauthenticatedMe(page: import('@playwright/test').Page) {
  await page.route('**/api/auth/me', async (route) => {
    await route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'unauthorized' }),
    });
  });
}

test.describe('auth routing', () => {
  test('unauthenticated /chat redirects to /login', async ({ page }) => {
    await mockUnauthenticatedMe(page);
    await page.goto('/chat');
    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByTestId('login-page')).toBeVisible();
  });

  test('unauthenticated /knowledge redirects to /login', async ({ page }) => {
    await mockUnauthenticatedMe(page);
    await page.goto('/knowledge');
    await expect(page).toHaveURL(/\/login/);
  });

  test('unauthenticated /admin/projects redirects to /login', async ({ page }) => {
    await mockUnauthenticatedMe(page);
    await page.goto('/admin/projects');
    await expect(page).toHaveURL(/\/login/);
  });

  test('login success lands on /chat', async ({ page }) => {
    test.skip(!hasAdminCredentials(), '需要 AUTH_LOCAL_ADMIN_SEED 或 E2E_ADMIN_*');
    const { username, password } = resolveAdminCredentials();
    await loginWithUI(page, username, password);
    await expect(page).toHaveURL(/\/chat/);
    await expect(page.getByText('你好！我是知识库问答助手')).toBeVisible();
  });

  test('login failure shows error', async ({ page }) => {
    // 登录页探测 me 时也模拟未登录，避免后端挂掉进入 error 态
    await mockUnauthenticatedMe(page);
    await page.route('**/api/auth/login', async (route) => {
      await route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'invalid credentials' }),
      });
    });
    await page.goto('/login');
    await page.fill('input[name="username"]', 'not-a-user');
    await page.fill('input[name="password"]', 'wrong-password');
    await page.click('button[type="submit"]');
    await expect(page.getByRole('alert')).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('alert')).toContainText('用户名或密码错误');
    await expect(page).toHaveURL(/\/login/);
  });

  test('authenticated /login redirects to /chat', async ({ page }) => {
    test.skip(!hasAdminCredentials(), '需要 AUTH_LOCAL_ADMIN_SEED 或 E2E_ADMIN_*');
    const { username, password } = resolveAdminCredentials();
    await loginViaApi(page, username, password);
    await page.goto('/login');
    await expect(page).toHaveURL(/\/chat/, { timeout: 15000 });
  });

  // 整页重载触发 AuthProvider 挂载时 /auth/me 初次探测（401 → unauthenticated）
  test('reload without session redirects to login', async ({ page }) => {
    test.skip(!hasAdminCredentials(), '需要 AUTH_LOCAL_ADMIN_SEED 或 E2E_ADMIN_*');
    const { username, password } = resolveAdminCredentials();
    await loginViaApi(page, username, password);
    await page.goto('/chat');
    await page.request.post('/api/auth/logout');
    await page.goto('/knowledge');
    await expect(page).toHaveURL(/\/login/, { timeout: 15000 });
  });

  // 运行时拦截：不 goto；在 SPA 内触发一次 apiJson
  test('in-app 401 triggers global redirect without reload', async ({ page }) => {
    test.skip(!hasAdminCredentials(), '需要 AUTH_LOCAL_ADMIN_SEED 或 E2E_ADMIN_*');
    const { username, password } = resolveAdminCredentials();
    await loginViaApi(page, username, password);
    await page.goto('/knowledge');
    await expect(page.getByTestId('knowledge-page')).toBeVisible({ timeout: 15000 });
    await page.request.post('/api/auth/logout');
    await page.getByTitle('刷新知识空间').click();
    await expect(page).toHaveURL(/\/login/, { timeout: 15000 });
  });

  test('non-admin cannot open admin projects', async ({ page }) => {
    test.skip(!hasAdminCredentials(), '需要 AUTH_LOCAL_ADMIN_SEED 或 E2E_ADMIN_*');
    const { username, password } = resolveAdminCredentials();
    await loginViaApi(page, username, password);

    const viewerUser = `e2e_viewer_${Date.now()}`;
    const viewerPass = 'viewer-pass-e2e';
    try {
      await createViewerUser(page, viewerUser, viewerPass);
    } catch (err) {
      test.skip(true, `无法创建 viewer 用户: ${err instanceof Error ? err.message : String(err)}`);
    }

    await page.request.post('/api/auth/logout');
    await loginViaApi(page, viewerUser, viewerPass);
    await page.goto('/chat');
    await expect(page.getByRole('link', { name: '项目管理' })).toHaveCount(0);
    await page.goto('/admin/projects');
    await expect(page.getByTestId('auth-forbidden')).toBeVisible({ timeout: 15000 });
    await expect(page.getByTestId('admin-projects-page')).toHaveCount(0);
  });
});
