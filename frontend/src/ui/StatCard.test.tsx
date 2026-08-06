/*
 * StatCard 测试（shared-shell 规格 §5）：卡面 token、标题/数值/说明行、
 * sparkline 折线（sienna 1.5px 无轴无网格）与分布条形（相对宽度）。
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { StatCard } from './StatCard';

describe('StatCard', () => {
  it('卡面：paper 底、elevated 圆角、subtle 阴影、20px padding', () => {
    const { container } = render(<StatCard title="Queries" value={128} />);
    const card = container.firstElementChild as HTMLElement;
    expect(card.className).toContain('bg-paper-white');
    expect(card.className).toContain('rounded-[var(--radius-elevatedcards)]');
    expect(card.className).toContain('shadow-[var(--shadow-subtle)]');
    expect(card.className).toContain('p-5');
  });

  it('标题 Sohne 500 20px + 主数值 + 15px slate 说明行', () => {
    render(<StatCard title="Queries" value={128} description="last 7 days" />);
    const title = screen.getByText('Queries');
    expect(title.className).toContain('text-[20px]');
    expect(title.className).toContain('font-medium');
    expect(screen.getByText('128')).toBeInTheDocument();
    expect(screen.getByText('last 7 days').className).toContain('text-slate-gray');
  });

  it('sparkline：渲染 sienna 1.5px 折线，无轴无网格；少于 2 点不渲染', () => {
    const { container, rerender } = render(<StatCard title="Q" value={1} sparkline={[1, 3, 2, 5]} />);
    const polyline = container.querySelector('polyline');
    expect(polyline).not.toBeNull();
    expect(polyline?.getAttribute('stroke')).toBe('var(--color-sienna-brown)');
    expect(polyline?.getAttribute('stroke-width')).toBe('1.5');
    expect(container.querySelectorAll('line, axis')).toHaveLength(0);

    rerender(<StatCard title="Q" value={1} sparkline={[4]} />);
    expect(container.querySelector('polyline')).toBeNull();
  });

  it('distribution：条形按最大值归一宽度，标签 15px slate', () => {
    const { container } = render(
      <StatCard
        title="Q"
        value={1}
        distribution={[
          { label: 'pdf', value: 8 },
          { label: 'md', value: 4 },
        ]}
      />,
    );
    const bars = container.querySelectorAll('.bg-sienna-brown');
    expect(bars).toHaveLength(2);
    expect(bars[0]).toHaveStyle({ width: '100%' });
    expect(bars[1]).toHaveStyle({ width: '50%' });
    expect(screen.getByText('pdf').className).toContain('text-slate-gray');
  });
});
