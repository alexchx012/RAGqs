import { test, expect } from '@playwright/test';
import {
  hasAdminCredentials,
  resolveAdminCredentials,
  loginViaApi,
} from './fixtures/auth';

test.describe('Chat functionality parity', () => {
  test.beforeEach(async ({ page }) => {
    test.skip(!hasAdminCredentials(), '需要 AUTH_LOCAL_ADMIN_SEED 或 E2E_ADMIN_*');
    const { username, password } = resolveAdminCredentials();
    await loginViaApi(page, username, password);
    await page.goto('/chat');
  });

  test('quick mode send and receive', async ({ page }) => {
    const input = page.locator('input[placeholder="输入你的问题..."]');
    await input.fill('你好');
    await input.press('Enter');
    await expect(page.locator('.message.assistant .message-content').first()).toBeVisible({
      timeout: 15000,
    });
  });

  test('mode dropdown shows stream option', async ({ page }) => {
    await page.click('text=快速');
    await expect(page.locator('text=流式')).toBeVisible();
  });

  test('send button disabled while input empty', async ({ page }) => {
    await expect(page.locator('.send-btn-circle')).toBeDisabled();
  });

  test('new chat button clears to greeting', async ({ page }) => {
    await page.click('text=新建对话');
    await expect(page.locator('text=你好！我是知识库问答助手')).toBeVisible();
  });

  test('file upload button exists on chat page', async ({ page }) => {
    await expect(page.locator('button[title="上传文件"]')).toBeVisible();
  });
});
