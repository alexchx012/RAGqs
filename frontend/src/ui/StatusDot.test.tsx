/*
 * StatusDot 测试（共用基座 §5.6 状态列、§2.5 脉冲动画）：intent 映射 token 类、可选脉冲。
 */

import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { StatusDot, type StatusDotIntent } from './StatusDot';

describe('StatusDot', () => {
  it('五种 intent 映射对应 bg token 类', () => {
    const expected: Record<StatusDotIntent, string> = {
      ink: 'bg-ink-black',
      slate: 'bg-slate-gray',
      success: 'bg-success',
      warning: 'bg-warning',
      danger: 'bg-danger',
    };
    for (const [intent, className] of Object.entries(expected)) {
      const { container, unmount } = render(<StatusDot intent={intent as StatusDotIntent} />);
      expect(container.firstElementChild?.className).toContain(className);
      unmount();
    }
  });

  it('默认无脉冲；pulse 时挂 ui-status-pulse 类', () => {
    const { container, rerender } = render(<StatusDot intent="slate" />);
    expect(container.firstElementChild?.className).not.toContain('ui-status-pulse');
    rerender(<StatusDot intent="slate" pulse />);
    expect(container.firstElementChild?.className).toContain('ui-status-pulse');
  });
});
