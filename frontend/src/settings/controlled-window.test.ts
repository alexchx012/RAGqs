import { afterEach, describe, expect, it, vi } from 'vitest';
import { openControlledWindow } from './controlled-window';

function fakeWindow() {
  const win = {
    opener: {} as Window | null,
    document: { write: vi.fn(), close: vi.fn() },
    location: { href: '' },
    close: vi.fn(),
  } as unknown as Window;
  return win;
}

describe('openControlledWindow（review：noopener 不再误判 blocked）', () => {
  const originalOpen = window.open;
  afterEach(() => {
    window.open = originalOpen;
  });

  it('成功打开时返回窗口引用并立即切断 opener（opener 置 null）', () => {
    const win = fakeWindow();
    window.open = vi.fn(() => win) as typeof window.open;

    const result = openControlledWindow();

    expect(result).toBe(win);
    expect(win.opener).toBeNull();
    // 不加 noopener 打开（否则返回 null 被误判 blocked）
    expect(window.open).toHaveBeenCalledWith('', '_blank');
  });

  it('window.open 抛错或返回 null 时返回 null（popup blocked）', () => {
    window.open = vi.fn(() => null) as typeof window.open;
    expect(openControlledWindow()).toBeNull();

    window.open = vi.fn(() => {
      throw new Error('blocked');
    }) as typeof window.open;
    expect(openControlledWindow()).toBeNull();
  });
});
