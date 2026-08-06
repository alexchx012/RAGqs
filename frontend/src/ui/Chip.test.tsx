/*
 * Chip 测试（共用基座 §3.3）：ghost pill 形态、展开箭头旋转与常驻底、非默认墨点。
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Chip } from './Chip';

describe('Chip', () => {
  it('默认收起态：透明底、发丝边、aria-expanded=false、无墨点', () => {
    render(<Chip>all scopes</Chip>);
    const chip = screen.getByRole('button', { name: /all scopes/ });
    expect(chip).toHaveAttribute('aria-expanded', 'false');
    expect(chip.className).toContain('border-[var(--color-hairline)]');
    expect(chip.className).toContain('bg-transparent');
    expect(chip.querySelector('.bg-ink-black')).toBeNull();
  });

  it('展开态：aria-expanded=true、mist 底常驻、箭头 rotate-180', () => {
    render(<Chip open>all scopes</Chip>);
    const chip = screen.getByRole('button', { name: /all scopes/ });
    expect(chip).toHaveAttribute('aria-expanded', 'true');
    expect(chip.className).toContain('bg-mist-gray');
    expect(chip.querySelector('svg')?.className.baseVal ?? '').toContain('rotate-180');
  });

  it('非默认：左侧 6px 墨色实心点', () => {
    render(<Chip nonDefault>two scopes</Chip>);
    const dot = screen.getByRole('button', { name: /two scopes/ }).querySelector('.bg-ink-black');
    expect(dot).not.toBeNull();
    expect(dot?.className).toContain('rounded-full');
  });
});
