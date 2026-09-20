/*
 * 设置表单底部操作区（drawer-visual-system）：独立顶边框 + fog 底，右对齐「取消」「保存」。
 * 取消为白底 hairline 描边，保存为 ink 实底白字；按钮高 36px（h-9），圆角走 --radius-buttons
 * （全局 9999px、抽屉作用域 8px，因此同一组件在抽屉内外各自呈现正确形状）。
 * 负外边距抵消 SettingsCard 的 p-8，使操作区横向铺满卡片内边缘并与卡片底边贴合。
 * 文案一律取自 copy，不在组件内散落字面量（文案纪律）。
 * saving 为真时禁用两个按钮，与既有偏好保存「保存中禁用相关控件」的语义一致。
 */
import { copy } from '../copy';

export interface FormFooterProps {
  onCancel: () => void;
  onSave: () => void;
  saving?: boolean;
  saveDisabled?: boolean;
  cancelLabel?: string;
  saveLabel?: string;
}

export function FormFooter({
  onCancel,
  onSave,
  saving = false,
  saveDisabled = false,
  cancelLabel = copy.controls.cancel,
  saveLabel = copy.controls.save,
}: FormFooterProps) {
  return (
    <div
      data-testid="form-footer"
      className="-mx-8 -mb-8 mt-2 flex justify-end gap-3 border-t border-hairline bg-fog-white px-8 py-4"
    >
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
