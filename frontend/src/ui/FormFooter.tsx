/*
 * 设置表单底部操作区（drawer-visual-system）：独立顶边框 + fog 底，右对齐「取消」「保存」。
 * 取消为白底 hairline 描边，保存为 ink 实底白字；按钮高 36px（h-9），圆角走 --radius-buttons
 * （全局 9999px、抽屉作用域 8px，因此同一组件在抽屉内外各自呈现正确形状）。
 * 负外边距抵消 SettingsCard 的 p-8，使操作区横向铺满卡片内边缘并与卡片底边贴合。
 * 文案一律取自 copy，不在组件内散落字面量（文案纪律）。
 * saving 为真时禁用两个按钮，与既有偏好保存「保存中禁用相关控件」的语义一致。
 * statusSlot 为可选前置插槽（如「已保存」小字）：渲染在按钮组之前并自带 mr-auto，把按钮组推到
 * 右侧、插槽留在左侧，从而与按钮同占页脚这一行——插槽出现不新增一行、不改变页脚高度与纵向节奏。
 * 不传该 prop 时页脚的类名与子节点与未加插槽前逐字节一致（常规设置等页面不传）。
 */
import type { ReactNode } from 'react';
import { copy } from '../copy';

export interface FormFooterProps {
  onCancel: () => void;
  onSave: () => void;
  saving?: boolean;
  saveDisabled?: boolean;
  cancelLabel?: string;
  saveLabel?: string;
  /** 按钮组左侧的前置插槽（如「已保存」反馈）；省略时页脚结构与既有实现完全一致。 */
  statusSlot?: ReactNode;
}

export function FormFooter({
  onCancel,
  onSave,
  saving = false,
  saveDisabled = false,
  cancelLabel = copy.controls.cancel,
  saveLabel = copy.controls.save,
  statusSlot,
}: FormFooterProps) {
  return (
    <div
      data-testid="form-footer"
      className="-mx-8 -mb-8 mt-2 flex justify-end gap-3 border-t border-hairline bg-fog-white px-8 py-4"
    >
      {statusSlot !== undefined && <div className="mr-auto flex items-center">{statusSlot}</div>}
      <button
        type="button"
        onClick={onCancel}
        disabled={saving}
        className="h-9 rounded-[var(--radius-buttons)] border border-hairline bg-paper-white px-4 text-caption text-ink-black disabled:opacity-60"
      >
        {cancelLabel}
      </button>
      <button
        type="button"
        onClick={onSave}
        disabled={saving || saveDisabled}
        className="h-9 rounded-[var(--radius-buttons)] bg-ink-black px-4 text-caption text-paper-white disabled:opacity-60"
      >
        {saveLabel}
      </button>
    </div>
  );
}
