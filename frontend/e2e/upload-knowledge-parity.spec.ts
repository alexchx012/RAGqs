import { test, expect } from '@playwright/test';

test.describe('Upload and knowledge panel parity', () => {
  test('management panel is visible with selector', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('.management-panel')).toBeVisible();
    await expect(page.locator('select.space-selector')).toBeVisible();
  });

  test('document list section exists', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('text=文档')).toBeVisible({ timeout: 10000 });
  });

  test('index job list section exists', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('text=索引任务')).toBeVisible({ timeout: 10000 });
  });

  test('audit list section exists', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('text=检索审计')).toBeVisible({ timeout: 10000 });
  });

  test('create space form is present', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('input[placeholder="space id"]')).toBeVisible();
    await expect(page.locator('input[placeholder="显示名称"]')).toBeVisible();
    await expect(page.locator('form.space-form button[type="submit"]')).toBeVisible();
  });

  test('file upload button exists', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('button[title="上传文件"]')).toBeVisible();
  });
});
