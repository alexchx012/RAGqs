/*
 * TextLink 测试（共用基座 §3.2/§4）：15px slate hover 下划线、danger 变体、禁用态。
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { TextLink } from './TextLink';

describe('TextLink', () => {
  it('默认 slate-gray，点击触发 onClick', async () => {
    const onClick = vi.fn();
    render(<TextLink onClick={onClick}>retry</TextLink>);
    const link = screen.getByRole('button', { name: 'retry' });
    expect(link.className).toContain('text-slate-gray');
    await userEvent.setup().click(link);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('danger 变体 text-danger', () => {
    render(<TextLink danger>revoke</TextLink>);
    expect(screen.getByRole('button', { name: 'revoke' }).className).toContain('text-danger');
  });

  it('disabled 态 smoke-gray 且不触发', async () => {
    const onClick = vi.fn();
    render(
      <TextLink disabled onClick={onClick}>
        prev
      </TextLink>,
    );
    const link = screen.getByRole('button', { name: 'prev' });
    expect(link).toBeDisabled();
    expect(link.className).toContain('disabled:text-smoke-gray');
    await userEvent.setup().click(link);
    expect(onClick).not.toHaveBeenCalled();
  });
});
