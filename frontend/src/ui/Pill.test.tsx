/*
 * Pill 控件测试（共用基座 §3.2/§5.6）：filled/ghost 变体、尺寸、disabled、loading 内联加载点。
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { Pill } from './Pill';

describe('Pill', () => {
  it('filled 默认：墨底白字、md 高 36', () => {
    render(<Pill>save</Pill>);
    const button = screen.getByRole('button', { name: 'save' });
    expect(button.className).toContain('bg-ink-black');
    expect(button.className).toContain('text-paper-white');
    expect(button.className).toContain('h-9');
  });

  it('ghost 变体：发丝边，ink 描边变体可切', () => {
    const { rerender } = render(<Pill variant="ghost">cancel</Pill>);
    const button = screen.getByRole('button', { name: 'cancel' });
    expect(button.className).toContain('border-[var(--color-hairline)]');
    rerender(
      <Pill variant="ghost" ghostBorder="ink">
        cancel
      </Pill>,
    );
    expect(button.className).toContain('border-ink-black');
  });

  it('danger 变体：红底白字', () => {
    render(<Pill danger>delete</Pill>);
    expect(screen.getByRole('button', { name: 'delete' }).className).toContain('bg-danger');
  });

  it('尺寸 sm/xs 高度 32/28', () => {
    const { rerender } = render(<Pill size="sm">a</Pill>);
    const button = screen.getByRole('button', { name: 'a' });
    expect(button.className).toContain('h-8');
    rerender(<Pill size="xs">a</Pill>);
    expect(button.className).toContain('h-7');
  });

  it('disabled：禁用态样式且不可点击', async () => {
    const onClick = vi.fn();
    render(
      <Pill disabled onClick={onClick}>
        save
      </Pill>,
    );
    const button = screen.getByRole('button', { name: 'save' });
    expect(button).toBeDisabled();
    expect(button.className).toContain('disabled:bg-mist-gray');
    await userEvent.setup().click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('loading：内容替换为内联加载点并禁用', () => {
    render(<Pill loading>save</Pill>);
    const button = screen.getByRole('button');
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-busy', 'true');
    expect(screen.queryByText('save')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveClass('loading-dots');
  });
});
