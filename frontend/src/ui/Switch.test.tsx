/*
 * Switch 测试（共用基座 §5.4）：Radix Switch 受控切换、键盘 Space 切换、状态样式。
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { Switch } from './Switch';

function Harness({ onChange }: { onChange?: (checked: boolean) => void }) {
  const [checked, setChecked] = useState(false);
  return (
    <Switch
      checked={checked}
      ariaLabel="privacy"
      onCheckedChange={(next) => {
        setChecked(next);
        onChange?.(next);
      }}
    />
  );
}

describe('Switch', () => {
  it('关 = mist 底；开 = ink 底', () => {
    const { rerender } = render(
      <Switch checked={false} onCheckedChange={() => {}} ariaLabel="privacy" />,
    );
    const control = screen.getByRole('switch');
    expect(control).toHaveAttribute('data-state', 'unchecked');
    expect(control.className).toContain('bg-mist-gray');

    rerender(<Switch checked onCheckedChange={() => {}} ariaLabel="privacy" />);
    expect(control).toHaveAttribute('data-state', 'checked');
    expect(control.className).toContain('data-[state=checked]:bg-ink-black');
  });

  it('点击与键盘 Space 均切换并回调', async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    const user = userEvent.setup();
    const control = screen.getByRole('switch');

    await user.click(control);
    expect(onChange).toHaveBeenLastCalledWith(true);
    expect(control).toHaveAttribute('data-state', 'checked');

    control.focus();
    await user.keyboard(' ');
    expect(onChange).toHaveBeenLastCalledWith(false);
    expect(control).toHaveAttribute('data-state', 'unchecked');
  });

  it('disabled 不可切换', async () => {
    const onChange = vi.fn();
    render(<Switch checked={false} disabled onCheckedChange={onChange} ariaLabel="privacy" />);
    await userEvent.setup().click(screen.getByRole('switch'));
    expect(onChange).not.toHaveBeenCalled();
  });
});
