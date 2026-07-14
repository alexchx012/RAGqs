import { test, expect } from '@playwright/test';

test.describe('Chat functionality parity', () => {
  test('quick mode send and receive', async ({ page }) => {
    await page.goto('/');
    const input = page.locator('input[placeholder="输入你的问题..."]');
    await input.fill('你好');
    await input.press('Enter');
    await expect(page.locator('.message.assistant .message-content').first()).toBeVisible({ timeout: 15000 });
  });

  test('mode dropdown shows stream option', async ({ page }) => {
    await page.goto('/');
    await page.click('text=快速');
    await expect(page.locator('text=流式')).toBeVisible();
  });

  test('send button disabled while input empty', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('.send-btn-circle')).toBeDisabled();
  });

  test('new chat button clears to greeting', async ({ page }) => {
    await page.goto('/');
    await page.click('text=新建对话');
    await expect(page.locator('text=你好！我是知识库问答助手')).toBeVisible();
  });
});
