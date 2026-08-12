/*
 * 管理面板 400px 模态框骨架（共用基座 §5.6 模态框架；与 ApprovalsModule 自定义对话框同规格）：
 * 遮罩 ink-black 24% + 居中卡片（w 400px、radius-elevatedcards、shadow-subtle-2）；
 * Esc 关闭 / Tab 圈定 / 首焦点与焦点恢复由 useModalDialog 承载；遮罩点击关闭。
 * 用户管理与部门管理的五个对话框共用，避免逐框复制骨架。
 */

import type { ReactNode } from 'react';
import { useModalDialog } from '../settings/use-modal-dialog';

export interface DialogFrameProps {
  readonly ariaLabel: string;
  readonly onClose: () => void;
  readonly children: ReactNode;
}

export function DialogFrame({ ariaLabel, onClose, children }: DialogFrameProps) {
  const dialogRef = useModalDialog(true, (open) => {
    if (!open) {
      onClose();
    }
  });
  return (
    <div
      ref={dialogRef}
      tabIndex={-1}
      className="fixed inset-0 z-50 outline-none"
      role="dialog"
      aria-modal="true"
      aria-label={ariaLabel}
    >
      <div className="fixed inset-0 bg-ink-black/24" onClick={onClose} aria-hidden="true" />
      <div
        className={
          'fixed top-1/2 left-1/2 w-[400px] max-w-[calc(100vw-32px)] ' +
          '-translate-x-1/2 -translate-y-1/2 ' +
          'rounded-[var(--radius-elevatedcards)] bg-paper-white p-5 shadow-[var(--shadow-subtle-2)]'
        }
      >
        {children}
      </div>
    </div>
  );
}
