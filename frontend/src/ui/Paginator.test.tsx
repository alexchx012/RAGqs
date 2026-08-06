/*
 * Paginator 测试（共用基座 §5.6）：页码指示文案、边界禁用态、翻页回调。
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { Paginator } from './Paginator';

describe('Paginator', () => {
  it('渲染页码指示：当前页 ink 480', () => {
    render(<Paginator page={2} totalPages={5} onChange={() => {}} />);
    const indicator = screen.getByText(copy.controls.pageIndicator(2, 5));
    expect(indicator.className).toContain('text-ink-black');
    expect(indicator.className).toContain('font-w480');
  });

  it('中间页：上下页均可点并回调对应页码', async () => {
    const onChange = vi.fn();
    render(<Paginator page={2} totalPages={5} onChange={onChange} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: copy.controls.paginatorPrev }));
    expect(onChange).toHaveBeenCalledWith(1);
    await user.click(screen.getByRole('button', { name: copy.controls.paginatorNext }));
    expect(onChange).toHaveBeenCalledWith(3);
  });

  it('首页禁用上一页、末页禁用下一页', async () => {
    const onChange = vi.fn();
    const { rerender } = render(<Paginator page={1} totalPages={3} onChange={onChange} />);
    const prev = screen.getByRole('button', { name: copy.controls.paginatorPrev });
    expect(prev).toBeDisabled();

    rerender(<Paginator page={3} totalPages={3} onChange={onChange} />);
    expect(screen.getByRole('button', { name: copy.controls.paginatorPrev })).toBeEnabled();
    expect(screen.getByRole('button', { name: copy.controls.paginatorNext })).toBeDisabled();
  });
});
