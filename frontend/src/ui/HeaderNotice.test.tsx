/*
 * HeaderNotice 测试：mist 提示条形态；neutral/success 停留 3s 开始淡出（--duration-fast），
 * 淡出结束回调 onDismiss；danger 驻留 8s 且带关闭按钮（审查 A3）。
 */

import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { copy } from '../copy';
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
    expect(notice.className).toContain('text-slate-strong');
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

  it('success：保持 3s 自动淡出（审查 A3 不回归）', () => {
    const onDismiss = vi.fn();
    render(<HeaderNotice message="published" intent="success" onDismiss={onDismiss} />);
    const notice = screen.getByRole('status');

    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(notice.className).toContain('opacity-0');
    act(() => {
      vi.advanceTimersByTime(150);
    });
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('danger：驻留 8s 才淡出，3s 时仍可见（审查 A3）', () => {
    const onDismiss = vi.fn();
    render(<HeaderNotice message="upload failed" intent="danger" onDismiss={onDismiss} />);
    const notice = screen.getByRole('status');

    act(() => {
      vi.advanceTimersByTime(7999);
    });
    expect(notice.className).toContain('opacity-100');
    expect(onDismiss).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(notice.className).toContain('opacity-0');

    act(() => {
      vi.advanceTimersByTime(150);
    });
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('danger：渲染关闭按钮，点击立即回调 onDismiss（审查 A3）', () => {
    const onDismiss = vi.fn();
    render(<HeaderNotice message="upload failed" intent="danger" onDismiss={onDismiss} />);

    fireEvent.click(screen.getByRole('button', { name: copy.a11y.dialogClose }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('neutral/success 不渲染关闭按钮，仅 danger 有（审查 A3）', () => {
    render(
      <>
        <HeaderNotice message="saved" />
        <HeaderNotice message="published" intent="success" />
      </>,
    );
    expect(screen.queryByRole('button', { name: copy.a11y.dialogClose })).toBeNull();
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
