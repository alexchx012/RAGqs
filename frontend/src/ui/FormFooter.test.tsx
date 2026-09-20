/*
 * FormFooter 测试（drawer-visual-system）：底部操作区——顶边框 + fog 底、右对齐
 * 「取消」（描边）/「保存」（ink 实底）；按钮高 36px、圆角走 --radius-buttons。
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { FormFooter } from './FormFooter';

describe('FormFooter', () => {
  it('顶边框 + fog 底 + 抵消卡片内边距，按钮右对齐', () => {
    render(<FormFooter onCancel={() => {}} onSave={() => {}} />);
    const bar = screen.getByTestId('form-footer');
    expect(bar.className).toContain('border-t');
    expect(bar.className).toContain('border-hairline');
    expect(bar.className).toContain('bg-fog-white');
    expect(bar.className).toContain('justify-end');
    expect(bar.className).toContain('-mx-8');
    expect(bar.className).toContain('-mb-8');
  });

  it('取消在左、保存在右；保存为 ink 实底、取消为白底 hairline 描边；高 36px 圆角走 token', () => {
    render(<FormFooter onCancel={() => {}} onSave={() => {}} />);
    const cancel = screen.getByRole('button', { name: copy.controls.cancel });
    const save = screen.getByRole('button', { name: copy.controls.save });
    expect(cancel.compareDocumentPosition(save) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(save.className).toContain('bg-ink-black');
    expect(save.className).toContain('h-9');
    expect(save.className).toContain('rounded-[var(--radius-buttons)]');
    expect(cancel.className).toContain('bg-paper-white');
    expect(cancel.className).toContain('border-hairline');
    expect(cancel.className).toContain('h-9');
  });

  it('默认文案取自 copy.controls（不得散落字面量）', () => {
    render(<FormFooter onCancel={() => {}} onSave={() => {}} />);
    expect(screen.getByRole('button', { name: copy.controls.cancel })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: copy.controls.save })).toBeInTheDocument();
  });

  it('saving 时两个按钮都禁用', () => {
    render(<FormFooter onCancel={() => {}} onSave={() => {}} saving />);
    expect(screen.getByRole('button', { name: copy.controls.cancel })).toBeDisabled();
    expect(screen.getByRole('button', { name: copy.controls.save })).toBeDisabled();
  });

  it('saveDisabled 只禁用保存键', () => {
    render(<FormFooter onCancel={() => {}} onSave={() => {}} saveDisabled />);
    expect(screen.getByRole('button', { name: copy.controls.cancel })).toBeEnabled();
    expect(screen.getByRole('button', { name: copy.controls.save })).toBeDisabled();
  });

  it('取消与保存各自触发回调', async () => {
    const onCancel = vi.fn();
    const onSave = vi.fn();
    render(<FormFooter onCancel={onCancel} onSave={onSave} />);
    await userEvent.click(screen.getByRole('button', { name: copy.controls.cancel }));
    await userEvent.click(screen.getByRole('button', { name: copy.controls.save }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledTimes(1);
  });
});
