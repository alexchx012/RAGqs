import { test, expect } from '@playwright/test';
import {
  hasAdminCredentials,
  resolveAdminCredentials,
  loginViaApi,
} from './fixtures/auth';

test.describe('Upload and knowledge panel parity', () => {
  test.beforeEach(async ({ page }) => {
    test.skip(!hasAdminCredentials(), '需要 AUTH_LOCAL_ADMIN_SEED 或 E2E_ADMIN_*');
    const { username, password } = resolveAdminCredentials();
    await loginViaApi(page, username, password);
    await page.goto('/knowledge');
  });

  test('management panel is visible with selector', async ({ page }) => {
    await expect(page.locator('.management-panel')).toBeVisible();
    await expect(page.locator('select.space-selector')).toBeVisible();
  });

  test('document list section exists', async ({ page }) => {
    await expect(page.locator('text=文档')).toBeVisible({ timeout: 10000 });
  });

  test('index job list section exists', async ({ page }) => {
    await expect(page.locator('text=索引任务')).toBeVisible({ timeout: 10000 });
  });

  test('personal knowledge page has no audit list', async ({ page }) => {
    await expect(page.locator('text=检索审计')).toHaveCount(0);
  });

  test('personal knowledge page has no create space form', async ({ page }) => {
    await expect(page.locator('form.space-form')).toHaveCount(0);
  });
});
