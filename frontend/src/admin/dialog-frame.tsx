/*
 * 管理面板 400px 模态框骨架（共用基座 §5.6 模态框架；与 ApprovalsModule 自定义对话框同规格）：
 * 遮罩 ink-black 24% + 居中卡片（w 400px、radius-elevatedcards、shadow-subtle-2）；
 * Esc 关闭 / Tab 圈定 / 首焦点与焦点恢复由 useModalDialog 承载；遮罩点击关闭。
 * 进出动画（审查 A35，与 ConfirmDialog 一致）：挂载即 data-state='open' 播 ui-dialog-enter；
 * Esc / 遮罩关闭先切 data-state='closed' 播 ui-dialog-exit（--duration-fast），动画结束后
 * 再通知 onClose 由父级卸载（父级因自身状态提前卸载时无动画，直接消失）。
 * 用户管理与部门管理的五个对话框共用，避免逐框复制骨架。
 */

import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useModalDialog } from '../settings/use-modal-dialog';

/** 退出动画时长：与 ui-dialog-exit / --duration-fast 一致（150ms）。 */
const EXIT_MS = 150;

export interface DialogFrameProps {
  readonly ariaLabel: string;
  readonly onClose: () => void;
  readonly children: ReactNode;
}

export function DialogFrame({ ariaLabel, onClose, children }: DialogFrameProps) {
  const [closing, setClosing] = useState(false);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const dialogRef = useModalDialog(true, () => setClosing(true));
  useEffect(() => {
    if (!closing) {
      return;
    }
    const timer = window.setTimeout(() => onCloseRef.current(), EXIT_MS);
    return () => window.clearTimeout(timer);
  }, [closing]);
  const state = closing ? 'closed' : 'open';
  return (
    <div
      ref={dialogRef}
      tabIndex={-1}
      data-state={state}
      className="ui-dialog-overlay fixed inset-0 outline-none"
      role="dialog"
      aria-modal="true"
      aria-label={ariaLabel}
    >
      <div className="fixed inset-0 bg-ink-black/24" onClick={() => setClosing(true)} aria-hidden="true" />
      <div
        data-state={state}
        className={
          'ui-dialog-content fixed top-1/2 left-1/2 w-[400px] max-w-[calc(100vw-32px)] ' +
          '-translate-x-1/2 -translate-y-1/2 ' +
          'rounded-[var(--radius-elevatedcards)] bg-paper-white p-5 shadow-[var(--shadow-subtle-2)]'
        }
      >
        {children}
      </div>
    </div>
  );
}
