/*
 * CountBadge 测试（共用基座 §5.6）：pill 形态徽标；计数为 0 不渲染。
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { CountBadge } from './CountBadge';

describe('CountBadge', () => {
  it('渲染计数：mist 底 pill、12px 480', () => {
    render(<CountBadge count={3} />);
    const badge = screen.getByText('3');
    expect(badge.className).toContain('bg-mist-gray');
    expect(badge.className).toContain('rounded-[var(--radius-buttons)]');
    expect(badge.className).toContain('font-w480');
  });

  it('计数为 0 不渲染', () => {
    const { container } = render(<CountBadge count={0} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('计数为负不渲染', () => {
    const { container } = render(<CountBadge count={-2} />);
    expect(container).toBeEmptyDOMElement();
  });
});
