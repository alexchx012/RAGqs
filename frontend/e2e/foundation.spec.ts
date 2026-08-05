import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { copy } from '../src/copy';

/*
 * e2e 骨架：自包含（不依赖后端），验证应用壳渲染、亮/暗主题机制、
 * reduced-motion 降级与 :focus-visible 基线。
 */

function readToken(page: Page, name: string): Promise<string> {
  return page.evaluate(
    (tokenName) => getComputedStyle(document.documentElement).getPropertyValue(tokenName).trim(),
    name,
  );
}

function readDataTheme(page: Page): Promise<string | undefined> {
  return page.evaluate(() => document.documentElement.dataset.theme);
}

declare global {
  interface Window {
    __themeProbe?: { duration: string | null };
    __themeObserver?: MutationObserver;
  }
}

/*
 * 主题切换过渡探针：在类出现的瞬间记录 <html> 的计算 transition-duration。
 * 过渡窗口只有 300ms，先断言再读取会漏掉窗口，因此用 MutationObserver 记录。
 * observer 挂到 window 上防止被 GC（无引用的 observer 会被回收）。
 */
async function installThemeProbe(page: Page): Promise<void> {
  await page.addInitScript(() => {
    window.__themeProbe = { duration: null };
    const install = () => {
      const element = document.documentElement;
      window.__themeObserver = new MutationObserver(() => {
        if (element.classList.contains('theme-switching')) {
          window.__themeProbe!.duration = getComputedStyle(element).transitionDuration;
        }
      });
      window.__themeObserver.observe(element, { attributes: true, attributeFilter: ['class'] });
    };
    // addInitScript 可能在 <html> 元素解析完成前执行，此时 documentElement 尚不存在
    if (document.documentElement) {
      install();
    } else {
      document.addEventListener('DOMContentLoaded', install, { once: true });
    }
  });
}

async function probeSwitchDuration(page: Page): Promise<number> {
  await page.evaluate(() => {
    window.__themeProbe!.duration = null;
  });
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect.poll(() => readDataTheme(page)).toBe('dark');
  await page.waitForFunction(() => window.__themeProbe?.duration !== null);
  const duration = await page.evaluate(() => window.__themeProbe!.duration!);
  return Number.parseFloat(duration);
}

test('shell renders placeholder page with copy from the single copy file', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveTitle(copy.appName);
  await expect(page.getByRole('heading', { name: copy.shell.placeholderTitle })).toBeVisible();
  await expect(page.getByText(copy.shell.placeholderBody)).toBeVisible();
});

test('unauthenticated page follows light system scheme with light token values', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('/');
  await expect.poll(() => readDataTheme(page)).toBe('light');
  await expect.poll(() => readToken(page, '--color-paper-white')).toBe('#ffffff');
  await expect.poll(() => readToken(page, '--color-ink-black')).toBe('#17191c');
  await expect.poll(() => readToken(page, '--color-hairline')).toBe('#ececec');
  // Tailwind 工具类经 @theme inline 解析到运行时变量（text-slate-gray → #777b86）
  await expect
    .poll(() => page.evaluate(() => getComputedStyle(document.querySelector('p')!).color))
    .toBe('rgb(119, 123, 134)');
});

test('same token names resolve to dark values under dark scheme (base doc 2.1 table)', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.goto('/');
  await expect.poll(() => readDataTheme(page)).toBe('dark');
  await expect.poll(() => readToken(page, '--color-paper-white')).toBe('#1c1f24');
  await expect.poll(() => readToken(page, '--color-ink-black')).toBe('#f2f2f3');
  await expect.poll(() => readToken(page, '--color-fog-white')).toBe('#23262c');
  await expect.poll(() => readToken(page, '--color-mist-gray')).toBe('#2c3038');
  await expect.poll(() => readToken(page, '--color-slate-gray')).toBe('#9aa0ab');
  await expect.poll(() => readToken(page, '--color-hairline')).toBe('#2f333b');
  await expect.poll(() => readToken(page, '--color-blush-peach')).toBe('#3a2d25');
  await expect.poll(() => readToken(page, '--color-sienna-brown')).toBe('#dfa88d');
  await expect.poll(() => readToken(page, '--color-danger')).toBe('#d1826f');
  await expect.poll(() => readToken(page, '--color-warning')).toBe('#d3a24f');
  await expect.poll(() => readToken(page, '--color-success')).toBe('#8ab69b');
  // 同一工具类 text-slate-gray 解析到暗色值（token 名不变换值）
  await expect
    .poll(() => page.evaluate(() => getComputedStyle(document.querySelector('p')!).color))
    .toBe('rgb(154, 160, 171)');
});

test('theme switch applies instantly with a 250ms site-wide color transition', async ({ page }) => {
  await installThemeProbe(page);
  await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'no-preference' });
  await page.goto('/');
  const seconds = await probeSwitchDuration(page);
  expect(seconds).toBeCloseTo(0.25, 2);
});

test('motion degrades to instant under prefers-reduced-motion', async ({ page }) => {
  await installThemeProbe(page);
  await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' });
  await page.goto('/');
  const seconds = await probeSwitchDuration(page);
  expect(seconds).toBeLessThan(0.001);
});

test('keyboard focus shows a 2px focus-visible outline', async ({ page }) => {
  await page.goto('/');
  await page.keyboard.press('Tab');
  const outline = await page.evaluate(() => {
    const element = document.activeElement;
    if (!element) {
      return null;
    }
    const style = getComputedStyle(element);
    return { width: style.outlineWidth, style: style.outlineStyle };
  });
  expect(outline).not.toBeNull();
  expect(outline!.width).toBe('2px');
  expect(outline!.style).toBe('solid');
});
