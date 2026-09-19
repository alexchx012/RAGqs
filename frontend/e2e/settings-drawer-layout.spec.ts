import { expect, test, type Locator, type Page } from '@playwright/test';
import { copy } from '../src/copy';

/*
 * 设置抽屉右栏布局与密码眼睛回归（fix-settings-scroll-and-password-eye）。
 *
 * 真实浏览器 + 契约 mock（MSW worker），账号 zhangsan / password123。
 * 断言四类已被实测确认的缺陷：
 * 1. 密码可见性「眼睛」按钮必须覆盖在输入框右侧并与输入框垂直居中
 *    （ui.css 的 .ui-touch-target 未分层规则曾压过 Tailwind 的 .absolute）。
 * 2. 右栏滚动容器不得出现横向溢出（不可见触控热区伪元素曾把它兑现成横向滚动条）。
 * 3. 切换模块时不得出现瞬时滚动条：过渡窗口内的 peak scrollHeight 不得高于
 *    离开/到达两侧 settle 高度的较大者（进入动画的 translateY(8px) 曾把它推高 8px）。
 * 4. 右栏不显示滚动条但保留滚动能力；内容列用满可用宽度。
 *
 * 文案纪律（src/test/copy-discipline.test.ts）扫描 e2e 目录的非注释内容：
 * 本文件不写字面中文断言文案，模块名一律取自 copy。
 */

test.use({ viewport: { width: 1440, height: 900 } });

/** 切换顺序刻意避开「点击当前已选模块」的空操作，保证每次都是真实过渡。 */
const SWITCH_ORDER = [
  copy.shell.drawer.modules.security,
  copy.shell.drawer.modules.appearance,
  copy.shell.drawer.modules.knowledge,
  copy.shell.drawer.modules.profile,
];

const VIEWPORT_WIDTH = 1440;
/** 内容列用满可用宽度：1440 视口下约 1084px，远高于旧的 724px 上限。 */
const MIN_CONTENT_WIDTH = VIEWPORT_WIDTH * 0.7;
/** 右侧留白：移除 720px 限宽后不应再出现 400px 级空白。 */
const MAX_RIGHT_GAP = 100;

interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

async function box(locator: Locator): Promise<Box> {
  // 认证会话 key 落地时抽屉内容子树会重挂载，先等可见再量，避免拿到已分离的旧节点。
  await locator.waitFor({ state: 'visible' });
  const measured = await locator.boundingBox();
  if (measured === null) {
    throw new Error('element has no bounding box');
  }
  return measured;
}

function centerY(measured: Box): number {
  return measured.y + measured.height / 2;
}

async function login(page: Page): Promise<void> {
  await page.goto('/login');
  await page.getByLabel(copy.login.usernameLabel, { exact: true }).fill('zhangsan');
  await page.getByLabel(copy.login.passwordLabel, { exact: true }).fill('password123');
  await page.getByRole('button', { name: copy.login.submit }).click();
  await expect(page.getByLabel(copy.chat.composer.inputPlaceholder)).toBeVisible();
}

function drawer(page: Page): Locator {
  return page.getByRole('dialog', { name: copy.shell.drawer.personalTitle });
}

/** 右栏内容滚动容器：左栏 <nav> 的相邻兄弟。 */
function contentPane(page: Page): Locator {
  return drawer(page).locator('nav + div');
}

async function selectModule(page: Page, name: string): Promise<void> {
  await drawer(page)
    .getByRole('navigation')
    .getByRole('button', { name, exact: true })
    .click();
}

test('login password eye sits inside the field and is vertically centred', async ({ page }) => {
  await page.goto('/login');
  const input = page.locator('#login-password');
  const field = input.locator('xpath=..');
  const eye = field.locator('button');
  await expect(eye).toBeVisible();

  const inputBox = await box(input);
  const fieldBox = await box(field);
  const eyeBox = await box(eye);

  // 垂直：眼睛中心与 40px 字段外框中心对齐（错位时低 11px）
  expect(Math.abs(centerY(eyeBox) - centerY(fieldBox))).toBeLessThanOrEqual(1.5);
  expect(eyeBox.y).toBeGreaterThanOrEqual(fieldBox.y - 0.5);
  expect(eyeBox.y + eyeBox.height).toBeLessThanOrEqual(fieldBox.y + fieldBox.height + 0.5);
  // 水平：落在输入框右侧，不与文字区重叠
  expect(eyeBox.x).toBeGreaterThan(inputBox.x + inputBox.width * 0.8);
});

test('security password eyes overlay the right of each input and stop inflating the field', async ({
  page,
}) => {
  await login(page);
  await page.goto('/settings/security');
  await expect(drawer(page)).toBeVisible();

  for (const id of [
    'settings-old-password',
    'settings-new-password',
    'settings-confirm-password',
  ]) {
    const input = drawer(page).locator(`#${id}`);
    await expect(input).toBeVisible();
    const field = input.locator('xpath=..');
    const eye = field.locator('button');

    const inputBox = await box(input);
    const fieldBox = await box(field);
    const eyeBox = await box(eye);

    // 覆盖在输入框内、垂直居中（错位时落在输入框左侧 8px 并溢出到框外）
    expect(
      Math.abs(centerY(eyeBox) - centerY(inputBox)),
      `${id} eye vertical centring`,
    ).toBeLessThanOrEqual(1.5);
    expect(eyeBox.x, `${id} eye on the right`).toBeGreaterThan(inputBox.x + inputBox.width - 48);
    expect(eyeBox.x + eyeBox.width).toBeLessThanOrEqual(inputBox.x + inputBox.width + 0.5);
    expect(eyeBox.y).toBeGreaterThanOrEqual(inputBox.y - 0.5);
    expect(eyeBox.y + eyeBox.height).toBeLessThanOrEqual(inputBox.y + inputBox.height + 0.5);
    // 眼睛脱离绝对定位时会把外框从 40px 撑到 64px
    expect(fieldBox.height, `${id} field height`).toBeLessThanOrEqual(inputBox.height + 1);
  }
});

test('every right pane has no horizontal overflow and uses the available width', async ({
  page,
}) => {
  await login(page);
  await page.goto('/settings');
  const pane = contentPane(page);
  await expect(pane).toBeVisible();

  for (const name of SWITCH_ORDER) {
    await selectModule(page, name);
    await page.waitForTimeout(700);
    const measured = await pane.evaluate((el) => {
      const rect = el.getBoundingClientRect();
      return {
        clientWidth: el.clientWidth,
        scrollWidth: el.scrollWidth,
        clientHeight: el.clientHeight,
        scrollHeight: el.scrollHeight,
        width: rect.width,
        rightGap: window.innerWidth - rect.right,
      };
    });
    expect
      .soft(measured.scrollWidth, `${name} horizontal overflow`)
      .toBeLessThanOrEqual(measured.clientWidth);
    expect.soft(measured.width, `${name} content column width`).toBeGreaterThan(MIN_CONTENT_WIDTH);
    expect.soft(measured.rightGap, `${name} right gap`).toBeLessThan(MAX_RIGHT_GAP);
    // 安全页内容确实高于可视区（滚动能力见另一条用例）；其余模块不得出现任何竖向溢出。
    if (name !== copy.shell.drawer.modules.security) {
      expect
        .soft(measured.scrollHeight, `${name} vertical overflow`)
        .toBeLessThanOrEqual(measured.clientHeight);
    }
  }
});

test('switching modules never inflates scrollHeight beyond the settled value', async ({ page }) => {
  await login(page);
  await page.goto('/settings');
  const pane = contentPane(page);
  await expect(pane).toBeVisible();

  // 等抽屉内容稳定后再开始，避免把认证会话重挂载当成过渡
  await page.waitForTimeout(1200);

  for (const name of SWITCH_ORDER) {
    const before = await pane.evaluate((el) => el.scrollHeight);
    // 点击前开始采样，覆盖整个过渡窗口
    const sampling = pane.evaluate(
      (el) =>
        new Promise<{ peak: number; settled: number }>((resolve) => {
          let peak = 0;
          const started = performance.now();
          const tick = (): void => {
            const height = el.scrollHeight;
            if (height > peak) {
              peak = height;
            }
            if (performance.now() - started < 1200) {
              requestAnimationFrame(tick);
            } else {
              resolve({ peak, settled: el.scrollHeight });
            }
          };
          tick();
        }),
    );
    await selectModule(page, name);
    const result = await sampling;
    // 过渡窗口里同时存在离开与到达两份内容：允许的最高值 = 两者 settle 高度的较大者。
    // 超出它即说明过渡容器/动画把 scrollHeight 人为推高了（修复前为 +8px）。
    const allowed = Math.max(before, result.settled);
    expect
      .soft(result.peak, `${name} peak scrollHeight (from ${before} to ${result.settled})`)
      .toBeLessThanOrEqual(allowed);
  }
});

test('right pane hides the scrollbar yet still scrolls to the end without clipping', async ({
  page,
}) => {
  await login(page);
  // 压到矮视口，确保安全页内容必然高于可视区——这样才真的测到「隐藏滚动条但保留滚动」，
  // 而不是内容恰好放得下时的空断言。
  await page.setViewportSize({ width: 1440, height: 560 });
  await page.goto('/settings/security');
  const pane = contentPane(page);
  await expect(pane).toBeVisible();

  const measured = await pane.evaluate((el) => ({
    scrollbarWidth: getComputedStyle(el).scrollbarWidth,
    maxScroll: el.scrollHeight - el.clientHeight,
  }));
  expect(measured.maxScroll, 'security pane overflows on a short viewport').toBeGreaterThan(0);
  expect(measured.scrollbarWidth, 'scrollbar is not displayed').toBe('none');

  // 滚动能力保留：能滚到底
  await pane.evaluate((el) => {
    el.scrollTop = el.scrollHeight;
  });
  await expect.poll(() => pane.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);

  // 且没有靠裁掉内容换「无滚动条」：最后一段标题完整落在可视区内
  const privacy = page.getByRole('heading', { name: copy.settings.security.privacyTitle });
  await expect(privacy).toBeVisible();
  const privacyBox = await box(privacy);
  const paneBox = await box(pane);
  expect(privacyBox.y).toBeGreaterThanOrEqual(paneBox.y - 0.5);
  expect(privacyBox.y + privacyBox.height).toBeLessThanOrEqual(paneBox.y + paneBox.height + 0.5);
});
