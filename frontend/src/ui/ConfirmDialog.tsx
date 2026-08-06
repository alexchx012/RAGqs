/*
 * 二次确认对话框（共用基座 §5.6 删除/恢复确认）：Radix Dialog 承载。
 * 宽 400px paper-white + radius-elevatedcards + shadow-subtle-2；遮罩 ink-black 24% 透明度；
 * 标题 20px 500 + 说明行 15px slate-gray；底部 ghost「取消」+ filled「确认」（danger 变体红底白字）。
 * 进入 opacity 0→1 + scale 0.97→1 --duration-base --ease-out，关闭反向（keyframes 在 ui.css）；
 * Esc / 遮罩 / 取消均关闭；焦点圈定与关闭后焦点返回由 Radix 自带；打开期间 useEscShield 挂空盾。
 */

import * as Dialog from '@radix-ui/react-dialog';
import { useRef } from 'react';
import { copy } from '../copy';
import { useEscShield } from '../lib/esc-stack-provider';
import { Pill } from './Pill';

export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /** 危险操作（删除等）：确认键红底白字（共用基座 §5.6）。 */
  danger?: boolean;
  onConfirm: () => void;
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  cancelLabel,
  danger = false,
  onConfirm,
}: ConfirmDialogProps) {
  useEscShield(open);
  // 受控用法无 Dialog.Trigger：渲染期（Radix 挂载并自动聚焦之前）记下触发焦点，
  // 关闭时在 onCloseAutoFocus 中恢复（preventDefault 跳过 Radix 对 Trigger 的默认聚焦）。
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  if (open && restoreFocusRef.current === null && document.activeElement instanceof HTMLElement) {
    restoreFocusRef.current = document.activeElement;
  }
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="ui-dialog-overlay fixed inset-0 bg-ink-black/24" />
        <Dialog.Content
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            restoreFocusRef.current?.focus();
            restoreFocusRef.current = null;
          }}
          className={
            'ui-dialog-content fixed top-1/2 left-1/2 w-[400px] max-w-[calc(100vw-32px)] ' +
            'rounded-[var(--radius-elevatedcards)] bg-paper-white p-5 ' +
            'shadow-[var(--shadow-subtle-2)]'
          }
        >
          <Dialog.Title className="text-[20px] font-medium text-ink-black">{title}</Dialog.Title>
          <Dialog.Description className="mt-2 text-[15px] text-slate-gray">
            {description}
          </Dialog.Description>
          <div className="mt-6 flex justify-end gap-2">
            <Pill variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
              {cancelLabel ?? copy.controls.cancel}
            </Pill>
            <Pill size="sm" danger={danger} onClick={onConfirm}>
              {confirmLabel ?? copy.controls.confirm}
            </Pill>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
