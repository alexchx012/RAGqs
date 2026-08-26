import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Orb } from './Orb';

/*
 * Orb 状态指示测试（动效 orbs B2）：默认装饰态 aria-hidden、--orb-k 按 28px 舞台缩放、
 * 三个绕轨圆点；带 label 时以 role=img 暴露可访问名。
 */
describe('Orb 状态指示', () => {
  it('默认 20px 装饰态：aria-hidden，--orb-k = size / 28', () => {
    const { container } = render(<Orb />);
    const root = container.firstElementChild as HTMLElement;
    expect(root).toHaveAttribute('aria-hidden', 'true');
    expect(root.style.width).toBe('20px');
    expect(root.style.height).toBe('20px');
    expect(root.style.getPropertyValue('--orb-k')).toBe(String(20 / 28));
    expect(container.querySelectorAll('.ui-orb-shape')).toHaveLength(3);
  });

  it('带 label 时以 role=img 暴露可访问名；size 可配', () => {
    render(<Orb size={16} label="正在检索" />);
    const orb = screen.getByRole('img', { name: '正在检索' });
    expect(orb.style.width).toBe('16px');
    expect(orb.style.getPropertyValue('--orb-k')).toBe(String(16 / 28));
  });
});
