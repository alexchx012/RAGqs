import { test, expect } from '@playwright/test';
import {
  hasAdminCredentials,
  resolveAdminCredentials,
  loginViaApi,
} from './fixtures/auth';

test.describe('admin projects', () => {
  test.beforeEach(async ({ page }) => {
    test.skip(!hasAdminCredentials(), '需要 AUTH_LOCAL_ADMIN_SEED 或 E2E_ADMIN_*');
    const { username, password } = resolveAdminCredentials();
    await loginViaApi(page, username, password);
    await page.goto('/admin/projects');
  });

  test('shows create space form and audit panel', async ({ page }) => {
    await expect(page.getByTestId('admin-projects-page')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('form.space-form')).toBeVisible();
    await expect(page.locator('text=检索审计')).toBeVisible();
  });

  test('shows user management panel without placeholder', async ({ page }) => {
    await expect(page.getByTestId('admin-projects-page')).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('用户管理')).toBeVisible();
    await expect(page.getByText('功能即将上线')).toHaveCount(0);
  });

  test('knowledge governance has no rename/delete space controls', async ({ page }) => {
    await expect(page.getByTestId('admin-projects-page')).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('button', { name: /重命名|删除空间|删除知识空间/ })).toHaveCount(0);
  });
});
