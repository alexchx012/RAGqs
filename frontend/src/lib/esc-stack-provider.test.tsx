import { StrictMode } from 'react';
import { fireEvent, render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { EscStackProvider, useEscLayer, useEscShield, useEscStack } from './esc-stack-provider';

function EscLayer({ onEscape, active = true }: { onEscape: () => void; active?: boolean }) {
  useEscLayer(onEscape, active);
  return null;
}

function EscShield({ active }: { active: boolean }) {
  useEscShield(active);
  return null;
}

function pressEscape() {
  fireEvent.keyDown(document, { key: 'Escape' });
}

describe('useEscLayer（抽屉/下钻层 Esc 登记）', () => {
  it('active 时按 Escape 触发回调', () => {
    const onEscape = vi.fn();
    render(
      <EscStackProvider>
        <EscLayer onEscape={onEscape} />
      </EscStackProvider>,
    );
    pressEscape();
    expect(onEscape).toHaveBeenCalledTimes(1);
  });

  it('active=false 时不登记，Esc 不触发回调', () => {
    const onEscape = vi.fn();
    render(
      <EscStackProvider>
        <EscLayer onEscape={onEscape} active={false} />
      </EscStackProvider>,
    );
    pressEscape();
    expect(onEscape).not.toHaveBeenCalled();
  });

  it('组件卸载后注销登记，Esc 不再触发回调', () => {
    const onEscape = vi.fn();
    const { rerender } = render(
      <EscStackProvider>
        <EscLayer onEscape={onEscape} />
      </EscStackProvider>,
    );
    pressEscape();
    expect(onEscape).toHaveBeenCalledTimes(1);
    rerender(<EscStackProvider>{null}</EscStackProvider>);
    pressEscape();
    expect(onEscape).toHaveBeenCalledTimes(1);
  });

  it('Provider 卸载后栈销毁，Esc 不再派发', () => {
    const onEscape = vi.fn();
    const { unmount } = render(
      <EscStackProvider>
        <EscLayer onEscape={onEscape} />
      </EscStackProvider>,
    );
    unmount();
    pressEscape();
    expect(onEscape).not.toHaveBeenCalled();
  });
});

describe('多层登记（Esc 只作用于栈顶一层）', () => {
  it('Esc 只触发后登记的栈顶层；顶层注销后逐层向上', () => {
    const bottom = vi.fn();
    const top = vi.fn();
    function Fixture({ showTop }: { showTop: boolean }) {
      return (
        <EscStackProvider>
          <EscLayer onEscape={bottom} />
          {showTop ? <EscLayer onEscape={top} /> : null}
        </EscStackProvider>
      );
    }
    const { rerender } = render(<Fixture showTop />);
    pressEscape();
    expect(top).toHaveBeenCalledTimes(1);
    expect(bottom).not.toHaveBeenCalled();

    rerender(<Fixture showTop={false} />);
    pressEscape();
    expect(top).toHaveBeenCalledTimes(1);
    expect(bottom).toHaveBeenCalledTimes(1);
  });
});

describe('useEscShield（Radix 浮层 Esc 盾牌）', () => {
  it('盾牌层吞掉 Esc；盾牌关闭后下层恢复响应', () => {
    const lower = vi.fn();
    function Fixture({ shieldOn }: { shieldOn: boolean }) {
      return (
        <EscStackProvider>
          <EscLayer onEscape={lower} />
          <EscShield active={shieldOn} />
        </EscStackProvider>
      );
    }
    const { rerender } = render(<Fixture shieldOn />);
    pressEscape();
    expect(lower).not.toHaveBeenCalled();

    rerender(<Fixture shieldOn={false} />);
    pressEscape();
    expect(lower).toHaveBeenCalledTimes(1);
  });
});

describe('StrictMode 双调用（回归：dispose 后必须重新挂接监听）', () => {
  it('双调用 effect 后 Esc 仍派发到登记层', () => {
    const onEscape = vi.fn();
    render(
      <StrictMode>
        <EscStackProvider>
          <EscLayer onEscape={onEscape} />
        </EscStackProvider>
      </StrictMode>,
    );
    pressEscape();
    expect(onEscape).toHaveBeenCalledTimes(1);
  });
});

describe('useEscStack（Provider 边界）', () => {
  it('在 EscStackProvider 外调用抛错', () => {
    function Bare() {
      useEscStack();
      return null;
    }
    // React 会把渲染期抛错写入 console.error，断言期间屏蔽噪音
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(() => render(<Bare />)).toThrow('useEscStack must be used within EscStackProvider');
    consoleError.mockRestore();
  });
});
