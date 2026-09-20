/*
 * useDraftForm 测试（drawer-visual-system）：控件只改本地草稿，「保存」才提交、「取消」丢弃；
 * 已提交快照变化（加载完成 / 保存成功 / 保存失败回滚）时草稿重置。
 */

import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useDraftForm } from './use-draft-form';

interface Prefs {
  theme: string;
  size: string;
}

const BASE: Prefs = { theme: 'system', size: 'standard' };

describe('useDraftForm', () => {
  it('set 改动草稿并置 dirty，不调用 save', () => {
    const save = vi.fn();
    const { result } = renderHook(() => useDraftForm<Prefs>(BASE, save));
    act(() => result.current.set({ theme: 'dark' }));
    expect(result.current.draft).toEqual({ theme: 'dark', size: 'standard' });
    expect(result.current.dirty).toBe(true);
    expect(save).not.toHaveBeenCalled();
  });

  it('commit 仅在 dirty 时调用 save，并传完整草稿', () => {
    const save = vi.fn();
    const { result } = renderHook(() => useDraftForm<Prefs>(BASE, save));
    act(() => result.current.commit());
    expect(save).not.toHaveBeenCalled();
    act(() => result.current.set({ size: 'large' }));
    act(() => result.current.commit());
    expect(save).toHaveBeenCalledWith({ theme: 'system', size: 'large' });
  });

  it('reset 丢弃草稿回到已提交值', () => {
    const { result } = renderHook(() => useDraftForm<Prefs>(BASE, vi.fn()));
    act(() => result.current.set({ theme: 'dark' }));
    act(() => result.current.reset());
    expect(result.current.draft).toEqual(BASE);
    expect(result.current.dirty).toBe(false);
  });

  it('已提交快照变化时草稿重置（保存成功与失败回滚的落点）', () => {
    const { result, rerender } = renderHook(
      ({ submitted }: { submitted: Prefs | null }) => useDraftForm<Prefs>(submitted, vi.fn()),
      { initialProps: { submitted: BASE as Prefs | null } },
    );
    act(() => result.current.set({ theme: 'dark' }));
    rerender({ submitted: { theme: 'light', size: 'standard' } });
    expect(result.current.draft).toEqual({ theme: 'light', size: 'standard' });
    expect(result.current.dirty).toBe(false);
  });

  it('submitted 为 null 时草稿为 null，set 与 commit 均为 no-op', () => {
    const save = vi.fn();
    const { result } = renderHook(() => useDraftForm<Prefs>(null, save));
    expect(result.current.draft).toBeNull();
    act(() => result.current.set({ theme: 'dark' }));
    expect(result.current.draft).toBeNull();
    act(() => result.current.commit());
    expect(save).not.toHaveBeenCalled();
  });

  it('set 只覆盖传入字段，不丢其它字段', () => {
    const { result } = renderHook(() => useDraftForm<Prefs>(BASE, vi.fn()));
    act(() => result.current.set({ theme: 'dark' }));
    act(() => result.current.set({ size: 'large' }));
    expect(result.current.draft).toEqual({ theme: 'dark', size: 'large' });
  });
});
