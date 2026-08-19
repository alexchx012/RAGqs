import { expect, test, type Page } from '@playwright/test';
import { copy } from '../src/copy';

/*
 * 抽屉左栏高亮竞态回归（fix-drawer-nav-highlight-race；shared-shell 规格 §1）。
 * 竞态时序守卫：页面内 MutationObserver + 25ms 间隔原子采样（同一 JS tick 读齐
 * URL / 页头 / 高亮，不存在跨帧拼接），快速连点六个管理模块，断言无一帧滞后。
 * 根因：react-router 默认 startTransition 包裹 location 更新，快速连切时被模块挂载
 * 数据加载等 urgent 更新抢占，抽屉 UI 滞后 URL 百毫秒级；修复为 BrowserRouter
 * useTransitions={false}（同步提交）。本用例在修复前稳定复现滞后帧，修复后为零。
 * vitest 侧逻辑不变量见 src/shell/drawer/drawer-nav-sync.test.tsx。
 */

const modules = copy.shell.drawer.modules;

async function login(page: Page, username: string): Promise<void> {
  await page.goto('/login');
  await page.getByLabel(copy.login.usernameLabel, { exact: true }).fill(username);
  await page.getByLabel(copy.login.passwordLabel, { exact: true }).fill('password123');
  await page.getByRole('button', { name: copy.login.submit }).click();
  await expect(page.getByLabel(copy.chat.composer.inputPlaceholder)).toBeVisible();
}

const adminModules = [
  { id: 'dashboard', title: modules.dashboard },
  { id: 'approvals', title: modules.approvals },
  { id: 'spaces', title: modules.spaces },
  { id: 'evaluation', title: modules.evaluation },
  { id: 'operations', title: modules.operations },
  { id: 'users', title: modules.usersOps },
];

declare global {
  interface Window {
    __navSamples?: string[];
  }
}

test('rapid admin module switching never leaves a stale nav highlight frame', async ({ page }) => {
  await login(page, 'ops-wang');
  await expect(page).toHaveURL(/\/admin\/dashboard$/);
  await expect(page.getByRole('dialog')).toBeVisible();

  await page.evaluate(() => {
    window.__navSamples = [];
    const record = () => {
      const dialog = document.querySelector('[role="dialog"]');
      if (dialog === null) return;
      const h1 = dialog.querySelector('h1')?.textContent?.trim() ?? '';
      const highlighted = Array.from(dialog.querySelectorAll('nav button'))
        .filter((button) => button.className.includes('font-w480'))
        .map((button) => button.textContent?.trim() ?? '')
        .join('|');
      window.__navSamples!.push(`${window.location.pathname} :: h1=${h1} nav=${highlighted}`);
    };
    new MutationObserver(record).observe(document.body, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ['class'],
    });
    setInterval(record, 25);
  });

  const dialog = page.getByRole('dialog');
  for (let round = 0; round < 4; round += 1) {
    for (const target of adminModules) {
      await dialog
        .locator('nav')
        .getByRole('button', { name: new RegExp(`^${target.title}`) })
        .first()
        .click({ timeout: 3000 });
      await page.waitForTimeout(80);
    }
  }
  await page.waitForTimeout(1200);

  const samples = await page.evaluate(() => window.__navSamples ?? []);
  const titleById = new Map(adminModules.map((module) => [module.id, module.title]));
  const staleFrames = samples.filter((sample) => {
    const match = sample.match(/^\/admin\/([\w-]+)/);
    if (match === null) return false;
    const expected = titleById.get(match[1]!);
    if (expected === undefined) return false;
    // 原子一致帧：h1 == 当前模块；左栏唯一高亮以模块名开头（可带计数徽标后缀）
    const navMatch = sample.match(/nav=(.*)$/);
    const nav = navMatch?.[1] ?? '';
    return !sample.includes(`h1=${expected}`) || !nav.startsWith(expected);
  });
  expect([...new Set(staleFrames)]).toEqual([]);
});
