/*
 * SegmentedControl 测试（共用基座 §3.3/§5.5）：受控渲染、点击切换、键盘左右方向键
 * roving tabindex、accent 段选中时滑块桃底棕文。
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { SegmentedControl, type SegmentedOption } from './SegmentedControl';

const OPTIONS: SegmentedOption[] = [
  { value: 'fast', label: 'fast' },
  { value: 'think', label: 'think' },
  { value: 'deep', label: 'deep', accent: true },
];

function Harness({ initial = 'fast', onChange }: { initial?: string; onChange?: (v: string) => void }) {
  const [value, setValue] = useState(initial);
  return (
    <SegmentedControl
      options={OPTIONS}
      value={value}
      ariaLabel="effort"
      onChange={(next) => {
        setValue(next);
        onChange?.(next);
      }}
    />
  );
}

describe('SegmentedControl', () => {
  it('渲染 radiogroup + 三个 radio，选中项 aria-checked 且 tabIndex=0，其余 -1', () => {
    render(<Harness initial="think" />);
    const radios = screen.getAllByRole('radio');
    expect(radios).toHaveLength(3);
    expect(screen.getByRole('radio', { name: 'think' })).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByRole('radio', { name: 'think' })).toHaveAttribute('tabIndex', '0');
    expect(screen.getByRole('radio', { name: 'fast' })).toHaveAttribute('tabIndex', '-1');
  });

  it('点击切换选中并回调 onChange', async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    await userEvent.setup().click(screen.getByRole('radio', { name: 'think' }));
    expect(onChange).toHaveBeenCalledWith('think');
    expect(screen.getByRole('radio', { name: 'think' })).toHaveAttribute('aria-checked', 'true');
  });

  it('键盘左右方向键切换并移动焦点（roving tabindex，含回绕）', async () => {
    const onChange = vi.fn();
    render(<Harness initial="fast" onChange={onChange} />);
    const user = userEvent.setup();
    screen.getByRole('radio', { name: 'fast' }).focus();

    await user.keyboard('{ArrowRight}');
    expect(onChange).toHaveBeenLastCalledWith('think');
    expect(screen.getByRole('radio', { name: 'think' })).toHaveFocus();

    await user.keyboard('{ArrowRight}');
    expect(onChange).toHaveBeenLastCalledWith('deep');
    expect(screen.getByRole('radio', { name: 'deep' })).toHaveFocus();

    // 末尾回绕到首位
    await user.keyboard('{ArrowRight}');
    expect(onChange).toHaveBeenLastCalledWith('fast');

    await user.keyboard('{ArrowLeft}');
    expect(onChange).toHaveBeenLastCalledWith('deep');
  });

  it('滑块按选中索引平移；accent 段选中时滑块桃底', () => {
    const { container, rerender } = render(
      <SegmentedControl options={OPTIONS} value="fast" onChange={() => {}} ariaLabel="effort" />,
    );
    const slider = () => container.querySelector('span[aria-hidden="true"]') as HTMLElement;
    expect(slider().style.transform).toBe('translateX(calc(0 * 100%))');
    expect(slider().className).toContain('bg-paper-white');

    rerender(<SegmentedControl options={OPTIONS} value="deep" onChange={() => {}} ariaLabel="effort" />);
    expect(slider().style.transform).toBe('translateX(calc(2 * 100%))');
    expect(slider().className).toContain('bg-blush-peach');
    expect(screen.getByRole('radio', { name: 'deep' }).className).toContain('text-sienna-brown');
  });
});
