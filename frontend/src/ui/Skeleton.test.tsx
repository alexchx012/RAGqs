/*
 * Skeleton 测试（共用基座 §3.2/§5.6 加载态）：行/卡/文本条形态与呼吸动画类。
 */

import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { SkeletonCard, SkeletonRow, SkeletonText } from './Skeleton';

describe('Skeleton', () => {
  it('SkeletonRow：高 40、mist 底、呼吸动画类', () => {
    const { container } = render(<SkeletonRow />);
    const row = container.firstElementChild;
    expect(row?.className).toContain('h-10');
    expect(row?.className).toContain('bg-mist-gray');
    expect(row?.className).toContain('ui-skeleton');
  });

  it('SkeletonText：按 width 渲染行条', () => {
    const { container } = render(<SkeletonText width="60%" />);
    expect(container.firstElementChild).toHaveStyle({ width: '60%' });
  });

  it('SkeletonCard：mist 卡内含 n 条骨架条', () => {
    const { container } = render(<SkeletonCard lines={4} />);
    const card = container.firstElementChild as HTMLElement;
    expect(card.className).toContain('rounded-[var(--radius-cards)]');
    expect(card.querySelectorAll('.ui-skeleton')).toHaveLength(4);
  });
});
