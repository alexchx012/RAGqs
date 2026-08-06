/*
 * 三态组件测试（共用基座 §3.2/§4/§5.6）：空态默认文案与覆盖、错误态重试链、加载骨架组合。
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FileWarning } from 'lucide-react';
import { describe, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { EmptyState, ErrorState, LoadingCards, LoadingRows } from './states';

describe('EmptyState', () => {
  it('默认 copy.states.empty 文案 + 24px 图标', () => {
    const { container } = render(<EmptyState />);
    expect(screen.getByText(copy.states.empty)).toBeInTheDocument();
    const icon = container.querySelector('svg');
    expect(icon?.classList.contains('text-smoke-gray')).toBe(true);
  });

  it('文案与图标可覆盖', () => {
    render(<EmptyState icon={FileWarning} text="nothing here" />);
    expect(screen.getByText('nothing here')).toBeInTheDocument();
  });
});

describe('ErrorState', () => {
  it('默认说明 + 重试文字链，点击触发 onRetry', async () => {
    const onRetry = vi.fn();
    render(<ErrorState onRetry={onRetry} />);
    expect(screen.getByText(copy.states.error)).toBeInTheDocument();
    await userEvent
      .setup()
      .click(screen.getByRole('button', { name: copy.states.retry }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('无 onRetry 时不渲染重试链', () => {
    render(<ErrorState />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});

describe('Loading 骨架组合', () => {
  it('LoadingRows 渲染 n 条骨架行', () => {
    const { container } = render(<LoadingRows count={5} />);
    expect(container.querySelectorAll('.ui-skeleton')).toHaveLength(5);
  });

  it('LoadingCards 渲染 n 张骨架卡', () => {
    const { container } = render(<LoadingCards count={2} />);
    expect(container.querySelectorAll('[aria-busy="true"] > div')).toHaveLength(2);
  });
});
