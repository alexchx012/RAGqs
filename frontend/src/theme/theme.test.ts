import { afterEach, describe, expect, it, vi } from 'vitest';
import { ThemeController, resolveTheme } from './theme';
import type { ThemeMedia, ThemeTarget } from './theme';

function createHarness(systemDark: boolean) {
  const listeners = new Set<(event: { matches: boolean }) => void>();
  const media: ThemeMedia & { emit(dark: boolean): void } = {
    matches: systemDark,
    addEventListener: (_type, listener) => {
      listeners.add(listener);
    },
    removeEventListener: (_type, listener) => {
      listeners.delete(listener);
    },
    emit(dark: boolean) {
      this.matches = dark;
      for (const listener of [...listeners]) {
        listener({ matches: dark });
      }
    },
  };
  const classes = new Set<string>();
  const target: ThemeTarget = {
    dataset: {},
    classList: {
      add: (...tokens: string[]) => {
        tokens.forEach((token) => classes.add(token));
      },
      remove: (...tokens: string[]) => {
        tokens.forEach((token) => classes.delete(token));
      },
    },
    style: { colorScheme: '' },
  };
  return { media, target, classes };
}

describe('resolveTheme', () => {
  it('system 偏好跟随系统亮/暗', () => {
    expect(resolveTheme('system', false)).toBe('light');
    expect(resolveTheme('system', true)).toBe('dark');
  });

  it('显式偏好覆盖系统设置', () => {
    expect(resolveTheme('light', true)).toBe('light');
    expect(resolveTheme('dark', false)).toBe('dark');
  });
});

describe('ThemeController', () => {
  afterEach(() => {
    vi.useRealTimers();
    // localStorage 跨测试共享：clear 防止显式偏好泄入后续用例的构造读取
    window.localStorage.clear();
  });

  it('默认偏好为 system：未登录页面跟随系统 prefers-color-scheme', () => {
    const { media, target } = createHarness(true);
    const controller = new ThemeController(target, media);
    expect(controller.getPreference()).toBe('system');
    expect(controller.getResolved()).toBe('dark');
    expect(target.dataset.theme).toBe('dark');
    expect(target.style.colorScheme).toBe('dark');
    controller.dispose();
  });

  it('系统偏好变化时跟随系统立即切换（token 名不变换值由 CSS 承载）', () => {
    const { media, target } = createHarness(false);
    const controller = new ThemeController(target, media);
    expect(target.dataset.theme).toBe('light');
    media.emit(true);
    expect(target.dataset.theme).toBe('dark');
    expect(target.style.colorScheme).toBe('dark');
    controller.dispose();
  });

  it('setPreference 立即生效，显式偏好后不再响应系统变化（登录后按用户 preferences）', () => {
    const { media, target } = createHarness(true);
    const controller = new ThemeController(target, media);
    controller.setPreference('light');
    expect(target.dataset.theme).toBe('light');
    expect(target.style.colorScheme).toBe('light');
    media.emit(false);
    expect(target.dataset.theme).toBe('light');
    controller.dispose();
  });

  it('切换时挂过渡类并在过渡窗口后移除（reduced-motion 由 CSS 降级）', () => {
    vi.useFakeTimers();
    const { media, target, classes } = createHarness(false);
    const controller = new ThemeController(target, media);
    controller.setPreference('dark');
    expect(classes.has('theme-switching')).toBe(true);
    vi.advanceTimersByTime(400);
    expect(classes.has('theme-switching')).toBe(false);
    controller.dispose();
  });

  it('同一 resolved 主题不会重复挂载切换过渡', () => {
    const { media, target, classes } = createHarness(false);
    const controller = new ThemeController(target, media);
    classes.delete('theme-switching');
    controller.setPreference('light');
    expect(classes.has('theme-switching')).toBe(false);
    controller.dispose();
  });

  it('构造时从 localStorage 恢复显式偏好（首屏与运行时同键）', () => {
    const { media, target } = createHarness(false);
    window.localStorage.setItem('ragqs-theme-preference', 'dark');
    const controller = new ThemeController(target, media);
    expect(controller.getPreference()).toBe('dark');
    expect(target.dataset.theme).toBe('dark');
    controller.dispose();
  });

  it('显式主题偏好写入 localStorage 供刷新首屏读取', () => {
    const { media, target } = createHarness(false);
    const controller = new ThemeController(target, media);
    controller.setPreference('dark');
    expect(window.localStorage.getItem('ragqs-theme-preference')).toBe('dark');
    controller.dispose();
  });

  it('dispose 后不再响应系统变化', () => {
    const { media, target } = createHarness(false);
    const controller = new ThemeController(target, media);
    controller.dispose();
    media.emit(true);
    expect(target.dataset.theme).toBe('light');
  });
});
