/*
 * 常规设置（外观）：主题与对话字号两项偏好走草稿-保存。
 * 分段控件只改本地草稿（useDraftForm），点「保存」才经 usePreferences 提交完整快照，
 * 「取消」丢弃草稿；保存失败由 use-preferences 回滚并置 saveError。
 * 界面语言与消息时间戳不在本期范围（后端无对应偏好字段），本模块不渲染也不留占位行。
 * 三态（loading / loadError / saveError）与 aria-busy 沿用既有语义。
 * FormFooter 是 SettingsCard 的直接子节点且必须留在最后（负外边距抵消卡片 p-8）。
 */
import { copy } from '../copy';
import { FormFooter } from '../ui/FormFooter';
import { FormRow } from '../ui/FormRow';
import { SegmentedControl, type SegmentedOption } from '../ui/SegmentedControl';
import { SettingsCard } from '../ui/SettingsCard';
import { useDraftForm } from './use-draft-form';
import { usePreferences } from './use-preferences';
import type { ChatFontSize, ThemePreferenceValue } from './types';

const THEME_OPTIONS: SegmentedOption[] = [
  { value: 'light', label: copy.settings.appearance.themeLight },
  { value: 'dark', label: copy.settings.appearance.themeDark },
  { value: 'system', label: copy.settings.appearance.themeSystem },
];

const FONT_SIZE_OPTIONS: SegmentedOption[] = [
  { value: 'standard', label: copy.settings.appearance.fontStandard },
  { value: 'large', label: copy.settings.appearance.fontLarge },
];

export function AppearanceModule() {
  const { preferences, loading, loadError, saveError, saving, reload, save } = usePreferences();
  // submitted 必须是稳定引用（preferences 是 use-preferences 的状态值，草稿保存后随之刷新）。
  // 传内联快照字面量会让每次渲染都重置草稿：草稿变化触发重渲染 → 新字面量 → 再重置，
  // 实测直接进入渲染死循环（用例挂住，非仅「吞掉输入」）。
  const { draft, set, reset, commit } = useDraftForm(preferences, save);

  return (
    <section
      aria-label={copy.settings.appearance.sectionLabel}
      aria-busy={loading || saving}
      className="pb-10"
    >
      {loading && (
        <p role="status" className="text-caption text-slate-strong">
          {copy.settings.appearance.loading}
        </p>
      )}

      {!loading && loadError && (
        <div role="alert" className="flex items-center gap-3">
          <p className="text-caption text-danger">{copy.settings.appearance.loadError}</p>
          <button
            type="button"
            onClick={reload}
            className="text-caption text-ink-black underline underline-offset-2"
          >
            {copy.settings.appearance.retry}
          </button>
        </div>
      )}

      {/* 加载态以 draft === null 判断：已提交快照未到位时 useDraftForm 不产出草稿。
          卡片整体不渲染，避免出现控件绑不上草稿的空壳。 */}
      {!loading && !loadError && draft !== null && (
        <SettingsCard ariaLabel={copy.settings.appearance.sectionLabel}>
          <FormRow
            label={copy.settings.appearance.themeTitle}
            description={copy.settings.appearance.themeDescription}
          >
            <SegmentedControl
              options={THEME_OPTIONS}
              value={draft.theme}
              onChange={(value) => set({ theme: value as ThemePreferenceValue })}
              ariaLabel={copy.settings.appearance.themeAria}
            />
          </FormRow>

          <FormRow
            label={copy.settings.appearance.fontSizeTitle}
            description={copy.settings.appearance.fontSizeDescription}
          >
            <SegmentedControl
              options={FONT_SIZE_OPTIONS}
              value={draft.chat_font_size}
              onChange={(value) => set({ chat_font_size: value as ChatFontSize })}
              ariaLabel={copy.settings.appearance.fontSizeAria}
            />
          </FormRow>

          {saveError && (
            <p role="alert" className="pt-4 text-caption text-danger">
              {copy.settings.appearance.saveError}
            </p>
          )}

          <FormFooter onCancel={reset} onSave={commit} saving={saving} />
        </SettingsCard>
      )}
    </section>
  );
}
