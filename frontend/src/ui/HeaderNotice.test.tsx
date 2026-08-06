/*
 * HeaderNotice 测试：mist 提示条形态；出现 3s 开始淡出（--duration-fast），淡出结束回调 onDismiss。
 */

import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { HeaderNotice } from './HeaderNotice';

describe('HeaderNotice', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('渲染提示条：mist 底、15px slate、role=status', () => {
    render(<HeaderNotice message="saved" />);
    const notice = screen.getByRole('status');
    expect(notice).toHaveTextContent('saved');
    expect(notice.className).toContain('bg-mist-gray');
    expect(notice.className).toContain('text-slate-gray');
    expect(notice.className).toContain('opacity-100');
  });

  it('3s 后进入淡出，再 150ms 后回调 onDismiss', () => {
    const onDismiss = vi.fn();
    render(<HeaderNotice message="saved" onDismiss={onDismiss} />);
    const notice = screen.getByRole('status');

    act(() => {
      vi.advanceTimersByTime(2999);
    });
    expect(notice.className).toContain('opacity-100');
    expect(onDismiss).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(notice.className).toContain('opacity-0');
    expect(onDismiss).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(150);
    });
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('卸载时清理定时器，不再回调', () => {
    const onDismiss = vi.fn();
    const { unmount } = render(<HeaderNotice message="saved" onDismiss={onDismiss} />);
    unmount();
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(onDismiss).not.toHaveBeenCalled();
  });
});
