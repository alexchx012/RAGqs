import { describe, expect, it } from 'vitest';
import { createEscStack } from './esc-stack';
import type { EscEventTarget } from './esc-stack';

function createTarget() {
  const listeners = new Set<(event: Event) => void>();
  const target: EscEventTarget & { dispatch(key: string): void } = {
    addEventListener: (_type, listener) => {
      listeners.add(listener);
    },
    removeEventListener: (_type, listener) => {
      listeners.delete(listener);
    },
    dispatch(key: string) {
      for (const listener of [...listeners]) {
        listener({ key } as KeyboardEvent);
      }
    },
  };
  return target;
}

describe('createEscStack（Esc 逐层向上关闭）', () => {
  it('Esc 只派发给当前最上层', () => {
    const target = createTarget();
    const stack = createEscStack(target);
    const calls: string[] = [];
    stack.push({ onEscape: () => calls.push('bottom') });
    stack.push({ onEscape: () => calls.push('top') });
    target.dispatch('Escape');
    expect(calls).toEqual(['top']);
    stack.dispose();
  });

  it('顶层注销后 Esc 落到下一层（逐层向上）', () => {
    const target = createTarget();
    const stack = createEscStack(target);
    const calls: string[] = [];
    stack.push({ onEscape: () => calls.push('bottom') });
    const popTop = stack.push({ onEscape: () => calls.push('top') });
    popTop();
    target.dispatch('Escape');
    expect(calls).toEqual(['bottom']);
    expect(stack.depth()).toBe(1);
    stack.dispose();
  });

  it('非 Esc 键不触发；空栈不触发', () => {
    const target = createTarget();
    const stack = createEscStack(target);
    const calls: string[] = [];
    target.dispatch('Escape');
    stack.push({ onEscape: () => calls.push('layer') });
    target.dispatch('Enter');
    expect(calls).toEqual([]);
    stack.dispose();
  });

  it('注销幂等；dispose 后不再派发', () => {
    const target = createTarget();
    const stack = createEscStack(target);
    const calls: string[] = [];
    const pop = stack.push({ onEscape: () => calls.push('layer') });
    pop();
    pop();
    expect(stack.depth()).toBe(0);
    stack.push({ onEscape: () => calls.push('late') });
    stack.dispose();
    target.dispatch('Escape');
    expect(calls).toEqual([]);
  });
});
