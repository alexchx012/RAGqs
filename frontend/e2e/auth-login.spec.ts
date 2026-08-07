import { expect, test } from '@playwright/test';
import { copy } from '../src/copy';

/*
 * 登录链路 e2e（fe-auth-login）：真实浏览器 + 契约 mock（MSW worker）。
 * 覆盖：未认证重定向、表单错误态、登录成功按角色落地、页面刷新后凭 refresh Cookie 静默恢复。
 * mock 账号见 src/mocks/auth-contract.ts fixtures（zhangsan / password123，role=user）。
 */

test('login success lands on the chat home, and a page reload silently restores the session', async ({
  page,
}) => {
  await page.goto('/');
  await expect(page).toHaveURL(/\/login$/);

  // 401 invalid_credentials：错误行就地出现，任一框再输入即时清除
  await page.getByLabel(copy.login.usernameLabel, { exact: true }).fill('zhangsan');
  await page.getByLabel(copy.login.passwordLabel, { exact: true }).fill('wrong-password');
  await page.getByRole('button', { name: copy.login.submit }).click();
  await expect(page.getByRole('alert')).toHaveText(copy.login.errorInvalidCredentials);
  await page.getByLabel(copy.login.passwordLabel, { exact: true }).fill('password123');
  await expect(page.getByRole('alert')).toHaveCount(0);

  // 成功：整页淡出后落地聊天主页
  await page.getByRole('button', { name: copy.login.submit }).click();
  await expect(page.getByLabel(copy.chat.composer.inputPlaceholder)).toBeVisible();

  // 页面刷新后 access token 丢失：静默 refresh 恢复会话，停留在主站而非回登录页
  await page.reload();
  await expect(page.getByLabel(copy.chat.composer.inputPlaceholder)).toBeVisible();
  await expect(page).not.toHaveURL(/\/login$/);
});

test('authenticated visit to /login redirects to the role landing page', async ({ page }) => {
  await page.goto('/login');
  await page.getByLabel(copy.login.usernameLabel, { exact: true }).fill('zhangsan');
  await page.getByLabel(copy.login.passwordLabel, { exact: true }).fill('password123');
  await page.getByRole('button', { name: copy.login.submit }).click();
  await expect(page.getByLabel(copy.chat.composer.inputPlaceholder)).toBeVisible();

  await page.goto('/login');
  await expect(page).not.toHaveURL(/\/login$/);
  await expect(page.getByLabel(copy.chat.composer.inputPlaceholder)).toBeVisible();
});
