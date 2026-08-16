import { act, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useSwipeClose } from './use-swipe-close';

function Harness({ onClose }: { onClose: () => void }) {
  const { panelProps } = useSwipeClose(onClose);
  return (
    <div {...panelProps}>
      <div data-swipe-scroll />
    </div>
  );
}

describe('useSwipeClose', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('从顶部向下拖过阈值后关闭面板', async () => {
    const onClose = vi.fn();
    const { container } = render(<Harness onClose={onClose} />);
    const panel = container.firstElementChild as HTMLDivElement;
    Object.defineProperty(panel, 'getBoundingClientRect', {
      configurable: true,
      value: () => ({ height: 500 }),
    });

    fireEvent.pointerDown(panel, { pointerId: 1, clientY: 0 });
    fireEvent.pointerMove(panel, { pointerId: 1, clientY: 100 });
    fireEvent.pointerUp(panel, { pointerId: 1, clientY: 100 });

    expect(panel.style.transform).toBe('translateY(100%)');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(250);
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('内容未滚到顶部时不接管向下拖动', () => {
    const onClose = vi.fn();
    const { container } = render(<Harness onClose={onClose} />);
    const panel = container.firstElementChild as HTMLDivElement;
    const scroller = panel.querySelector<HTMLElement>('[data-swipe-scroll]');

    if (scroller === null) throw new Error('swipe scroller is missing');
    scroller.scrollTop = 20;
    fireEvent.pointerDown(panel, { pointerId: 1, clientY: 0 });
    fireEvent.pointerMove(panel, { pointerId: 1, clientY: 100 });
    fireEvent.pointerUp(panel, { pointerId: 1, clientY: 100 });

    expect(panel.style.transform).toBe('');
    expect(onClose).not.toHaveBeenCalled();
  });
});
