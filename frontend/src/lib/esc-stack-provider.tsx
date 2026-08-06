/*
 * Esc 全局交互栈的 React 绑定（shared-shell 规格 §7）。
 * - 抽屉 / 下钻层经 useEscLayer 登记，Esc 只作用于栈顶一层。
 * - Radix 无头原语（菜单、对话框、面板）内部处理自身 Esc：打开期间经 useEscShield
 *   在栈顶登记一层空盾，阻止 Esc 同时穿透关闭下层抽屉（栈在 Radix dismiss 层之上统一协调）。
 */

import { createContext, useContext, useEffect, useRef, type ReactNode } from 'react';
import { createEscStack, type EscStack } from './esc-stack';

const EscStackContext = createContext<EscStack | null>(null);

export function EscStackProvider({ children }: { children: ReactNode }) {
  const ref = useRef<EscStack | null>(null);
  if (ref.current === null) {
    ref.current = createEscStack(document);
  }
  useEffect(() => {
    const stack = ref.current;
    // StrictMode 双调用：第一轮 cleanup dispose 摘监听，第二轮 setup 必须重新挂接
    stack?.attach();
    return () => stack?.dispose();
  }, []);
  return <EscStackContext.Provider value={ref.current}>{children}</EscStackContext.Provider>;
}

export function useEscStack(): EscStack {
  const stack = useContext(EscStackContext);
  if (stack === null) {
    throw new Error('useEscStack must be used within EscStackProvider');
  }
  return stack;
}

/** 登记一层 Esc 处理；active 为 false 或卸载时注销。 */
export function useEscLayer(onEscape: () => void, active = true): void {
  const stack = useEscStack();
  const ref = useRef(onEscape);
  ref.current = onEscape;
  useEffect(() => {
    if (!active) {
      return;
    }
    return stack.push({ onEscape: () => ref.current() });
  }, [stack, active]);
}

/** Radix 浮层打开期间挂空盾：Esc 由 Radix 自身关闭浮层，不再下传到抽屉 / 下钻层。 */
export function useEscShield(active: boolean): void {
  useEscLayer(() => undefined, active);
}
