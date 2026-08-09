/*
 * 自定义模态的 Esc/focus/Tab 行为（review D16 + Minor13）：与既有 ConfirmDialog
 * （Radix Dialog + useEscShield）兼容——打开期间在 Esc 栈挂空盾（阻止 Esc 穿透关闭下层抽屉），
 * Esc 关闭本模态；打开时首焦点落入对话框；Tab 在框内循环；关闭后焦点恢复到打开前元素。
 * onOpenChange 用 ref 承载：effect 只在 open 翻转时挂/卸监听，不因内联回调每次渲染重跑，
 * 避免 keystroke 触发的重渲染把焦点拽回触发元素。
 * 返回 dialogRef 挂到模态容器（容器应带 tabIndex={-1} 与 outline-none）。
 */

import { useEffect, useRef, type RefObject } from 'react';
import { useEscShield } from '../lib/esc-stack-provider';

const FOCUSABLE_SELECTOR =
  'button:not([disabled]), input:not([disabled]), [href], select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusableIn(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) => element.offsetParent !== null || element === document.activeElement,
  );
}

export function useModalDialog(open: boolean, onOpenChange: (open: boolean) => void): RefObject<HTMLDivElement | null> {
  useEscShield(open);
  const onOpenChangeRef = useRef(onOpenChange);
  onOpenChangeRef.current = onOpenChange;
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const wasOpenRef = useRef(false);

  useEffect(() => {
    if (open === wasOpenRef.current) {
      return;
    }
    wasOpenRef.current = open;
    if (open) {
      // 打开时记录触发焦点（与 ConfirmDialog 一致）
      if (document.activeElement instanceof HTMLElement) {
        restoreFocusRef.current = document.activeElement;
      }
      // 首焦点：落入对话框内第一个可聚焦元素（无则对话框容器）
      const container = dialogRef.current;
      if (container !== null) {
        const first = focusableIn(container)[0] ?? container;
        first.focus();
      }
      const onKeyDown = (event: KeyboardEvent) => {
        if (event.key === 'Escape') {
          event.stopPropagation();
          onOpenChangeRef.current(false);
          return;
        }
        if (event.key !== 'Tab') {
          return;
        }
        const container = dialogRef.current;
        if (container === null) {
          return;
        }
        const focusable = focusableIn(container);
        if (focusable.length === 0) {
          event.preventDefault();
          return;
        }
        const first = focusable[0]!;
        const last = focusable[focusable.length - 1]!;
        const active = document.activeElement;
        if (event.shiftKey) {
          if (active === first || active === container || !container.contains(active)) {
            event.preventDefault();
            last.focus();
          }
        } else if (active === last || active === container || !container.contains(active)) {
          event.preventDefault();
          first.focus();
        }
      };
      document.addEventListener('keydown', onKeyDown, true);
      return () => {
        document.removeEventListener('keydown', onKeyDown, true);
      };
    }
    // 关闭：焦点恢复到打开前元素（仅当由 open→closed 翻转时执行一次）
    restoreFocusRef.current?.focus();
    restoreFocusRef.current = null;
    return undefined;
  }, [open]);

  return dialogRef;
}
