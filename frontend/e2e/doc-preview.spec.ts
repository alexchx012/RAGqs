import { expect, test, type Locator, type Page } from '@playwright/test';
import { copy } from '../src/copy';
import { CHAT_SEED_DOCUMENT_NAMES } from '../src/mocks/chat-contract';
import { PREVIEW_SEED } from '../src/mocks/preview-contract';

/*
 * 原文预览页 e2e（fe-doc-preview）：真实浏览器 + 契约 mock（MSW worker），自包含不起后端。
 * 覆盖关键路径：
 * - 引用角标点击 → 新窗口 → 预览加载（真实 pdfjs 解析种子 PDF，验证夹具可解析性）
 *   → 命中导航点击切换高亮；
 * - 扫描件只跳页无片段高亮；Excel 页签与 a1 高亮；无 message_id 只读空态；不可用态不泄露元数据。
 * 文案一律经 copy / 种子常量引用（copy-discipline：e2e 禁 CJK 字面量）。
 */

async function login(page: Page, username = 'zhangsan'): Promise<void> {
  await page.goto('/login');
  await page.getByLabel(copy.login.usernameLabel, { exact: true }).fill(username);
  await page.getByLabel(copy.login.passwordLabel, { exact: true }).fill('password123');
  await page.getByRole('button', { name: copy.login.submit }).click();
  await expect(page.getByLabel(copy.chat.composer.inputPlaceholder)).toBeVisible();
}

async function draggablePanelBox(page: Page, panel: Locator): Promise<{ x: number; y: number; width: number; height: number }> {
  const viewportHeight = page.viewportSize()?.height ?? 0;
  await expect.poll(async () => {
    const box = await panel.boundingBox();
    return box === null ? Number.POSITIVE_INFINITY : box.y + 24;
  }).toBeLessThan(viewportHeight);
  return (await panel.boundingBox()) as { x: number; y: number; width: number; height: number };
}

test('citation click opens preview in a new window, and hit nav switches the current highlight', async ({
  page,
  context,
}) => {
  await login(page);
  const composer = page.getByLabel(copy.chat.composer.inputPlaceholder);

  // 提问（quick 档：mock answer 携带一条 page 引用 doc_1）
  await composer.fill('annual leave policy question');
  await page.getByRole('button', { name: copy.chat.composer.sendAria }).click();
  const answerBlock = page
    .locator('.chat-message-enter')
    .filter({ hasText: 'Mock answer for' })
    .first();
  await expect(answerBlock).toBeVisible();

  // 点击角标：新窗口打开 /preview/doc_1（message_id + document_version_id 透传）
  const badge = answerBlock.getByRole('button', { name: copy.chat.message.citeOpenAria }).first();
  const [preview] = await Promise.all([
    context.waitForEvent('page'),
    badge.click({ modifiers: ['Control'] }),
  ]);
  await preview.waitForLoadState();
  expect(preview.url()).toContain('/preview/doc_1');
  expect(preview.url()).toContain('message_id=');
  expect(preview.url()).toContain('document_version_id=v_1');

  // 页头：文档名（Signifier 44px）+ 载体类型标签
  await expect(preview.getByRole('heading', { level: 1 })).toHaveText(CHAT_SEED_DOCUMENT_NAMES.employeeHandbook);
  await expect(preview.getByText(copy.preview.mediaKind.pdf)).toBeVisible();

  // 命中导航 2 条；打开自动高亮第一处命中（真实 pdfjs 文本层匹配 snippet）
  const nav = preview.getByLabel(copy.preview.navAria);
  await expect(nav.getByRole('button')).toHaveCount(2);
  await expect(preview.locator('.preview-hit--current')).toHaveText('5 days per year');

  // 点击第二条命中：当前高亮切换（旧命中过渡为浅标）
  await nav.getByRole('button').nth(1).click();
  await expect(preview.locator('.preview-hit--current')).toHaveText('medical certificate');
  await expect(preview.locator('.preview-hit')).toHaveCount(2);
  await expect(nav.getByRole('button').nth(1)).toHaveAttribute('aria-current', 'true');
});

test('scanned PDF jumps to the hit page without fragment highlight or substitute anchors', async ({ page }) => {
  await login(page);
  await page.goto(`/preview/${PREVIEW_SEED.scanDocId}?message_id=m_1`);

  await expect(page.getByRole('heading', { level: 1 })).toHaveText(PREVIEW_SEED.scanDocName);
  // 命中页容器渲染（真实 pdfjs 解析无文本操作的扫描件夹具）；限定 .preview-pdf-page 避开 react-pdf 自带同名属性
  await expect(page.locator('.preview-pdf-page[data-page-number="1"]')).toBeVisible();
  const nav = page.getByLabel(copy.preview.navAria);
  await expect(nav.getByRole('button')).toHaveCount(1);
  // 只跳页：无片段高亮、无任何替代锚点 UI
  await expect(page.locator('mark.preview-hit')).toHaveCount(0);
  await expect(page.locator('[data-hit-anchor]')).toHaveCount(0);
});

test('historical PDF keeps the preview-selected version in its content request', async ({ page }) => {
  await login(page);
  const contentRequest = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return (
      url.pathname === '/v1/documents/doc_1/content' &&
      url.searchParams.get('document_version_id') === 'v_0'
    );
  });

  await page.goto('/preview/doc_1?message_id=m_1&document_version_id=v_0');
  await expect(page.getByRole('heading', { level: 1 })).toHaveText(CHAT_SEED_DOCUMENT_NAMES.employeeHandbook);

  const request = await contentRequest;
  const url = new URL(request.url());
  expect(url.searchParams.getAll('document_version_id')).toEqual(['v_0']);
});

test('excel preview shows source sheet tabs, a1 highlight, and ?sheet= switching', async ({ page }) => {
  await login(page);
  await page.goto(`/preview/${PREVIEW_SEED.excelDocId}?message_id=m_1`);

  await expect(page.getByRole('heading', { level: 1 })).toHaveText(PREVIEW_SEED.excelDocName);
  const tabs = page.getByRole('radiogroup', { name: copy.preview.sheetTabsAria });
  await expect(tabs.getByRole('radio', { name: PREVIEW_SEED.excelSheetQ1 })).toHaveAttribute('aria-checked', 'true');
  // 打开跳到首个命中 Sheet：A2:C2 三格当前高亮（表头 mist-gray 底行存在）
  await expect(page.locator('.preview-sheet-table thead th').first()).toBeVisible();
  await expect(page.locator('td.preview-hit-cell--current')).toHaveCount(3);

  // ?sheet= 切换：URL 更新；非激活 Sheet 命中格呈浅标，当前命中不在本表
  await tabs.getByRole('radio', { name: PREVIEW_SEED.excelSheetQ2 }).click();
  await expect(page).toHaveURL(/sheet=Q2/);
  await expect(page.locator('td.preview-hit-cell--current')).toHaveCount(0);
  await expect(page.locator('td.preview-hit-cell')).toHaveCount(1);

  // 点击第二条命中：Q2 A2 单格转当前高亮
  const nav = page.getByLabel(copy.preview.navAria);
  await nav.getByRole('button').nth(1).click();
  await expect(page.locator('td.preview-hit-cell--current')).toHaveCount(1);
  await expect(page.locator('td.preview-hit-cell--current')).toHaveText(PREVIEW_SEED.excelQ2FirstCell);
});

test('without message_id the preview is read-only: nav empty state and no highlights', async ({ page }) => {
  await login(page);
  await page.goto(`/preview/${PREVIEW_SEED.excelDocId}`);

  await expect(page.getByRole('heading', { level: 1 })).toHaveText(PREVIEW_SEED.excelDocName);
  await expect(page.getByText(copy.preview.navEmpty)).toBeVisible();
  await expect(page.locator('.preview-sheet-table').first()).toBeVisible();
  await expect(page.locator('td.preview-hit-cell')).toHaveCount(0);
});

test('unavailable document shows only the unavailable notice and leaks no metadata', async ({ page }) => {
  await login(page);
  await page.goto(`/preview/${PREVIEW_SEED.goneDocId}?message_id=m_1`);

  await expect(page.getByText(copy.preview.unavailable)).toBeVisible();
  // 不显示文档名、命中导航、原文或下载入口
  await expect(page.getByText(PREVIEW_SEED.goneDocName)).toHaveCount(0);
  await expect(page.getByLabel(copy.preview.navAria)).toHaveCount(0);
  await expect(page.locator('mark.preview-hit')).toHaveCount(0);
});

test('narrow viewport: hit nav panel closes via swipe-down gesture (fe-preview-swipe-close)', async ({
  browser,
}) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true });
  const page = await context.newPage();
  try {
    await login(page);
    await page.goto(`/preview/${PREVIEW_SEED.excelDocId}?message_id=m_1`);
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(PREVIEW_SEED.excelDocName);

    // 窄屏：桌面侧栏不渲染，「命中点 N」按钮打开底部半屏面板
    await expect(page.getByLabel(copy.preview.navAria)).toHaveCount(0);
    await page.getByRole('button', { name: copy.preview.navTitle(2) }).click();
    const panel = page.getByRole('dialog');
    await expect(panel).toBeVisible();

    // 下滑手势（pointer 序列：处理器接受任意 pointer 类型；dy=200 ≥ 阈值 80）
    const box = await draggablePanelBox(page, panel);
    const startX = box.x + box.width / 2;
    const startY = box.y + 24;
    await page.mouse.move(startX, startY);
    await page.mouse.down();
    await page.mouse.move(startX, startY + 200, { steps: 10 });
    await page.mouse.up();
    await expect(panel).toHaveCount(0);

    // 再次打开：面板复位（无 translateY 内联残留），且手势仍可再次关闭
    await page.getByRole('button', { name: copy.preview.navTitle(2) }).click();
    const panel2 = page.getByRole('dialog');
    await expect(panel2).toBeVisible();
    await expect(panel2).not.toHaveAttribute('style', /translateY/);
    const box2 = await draggablePanelBox(page, panel2);
    const startX2 = box2.x + box2.width / 2;
    const startY2 = box2.y + 24;
    await page.mouse.move(startX2, startY2);
    await page.mouse.down();
    await page.mouse.move(startX2, startY2 + 200, { steps: 10 });
    await page.mouse.up();
    await expect(panel2).toHaveCount(0);
  } finally {
    await context.close();
  }
});
