/*
 * 常规设置（外观）：主题与对话字号两项偏好走草稿-保存。
 * 分段控件只改本地草稿（useDraftForm），点「保存」才经 usePreferences 提交完整快照，
 * 「取消」丢弃草稿；保存失败由 use-preferences 回滚并置 saveError。
 * 界面语言与消息时间戳不在本期范围（后端无对应偏好字段），本模块不渲染也不留占位行。
 * 三态（loading / loadError / saveError）与 aria-busy 沿用既有语义。
 * 保存进行中禁用两个分段控件（见 DraftFieldSet）：use-preferences 的 saving 契约要求消费方
 * 禁用相关控件，且保存落地会重置草稿，窗口内可改会导致在途编辑被静默丢弃。
 * FormFooter 是 SettingsCard 的直接子节点且必须留在最后（负外边距抵消卡片 p-8）。
 */
import type { ReactNode } from 'react';
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

/*
 * 分段控件的禁用外壳。保存进行中必须禁用可交互控件：use-preferences 对 saving 的契约是
 * 「消费方据此禁用相关控件」；且真实保存响应每次都是新对象（api/client.ts 的 response.json()），
 * useDraftForm 见 submitted 身份变化即重置草稿——窗口内若还能改，那笔在途编辑会被静默丢弃。
 * fieldset 原生 disabled 让后代控件不可交互；disabled:opacity-60 + disabled:cursor-not-allowed
 * 让禁用态可见。enabled:opacity-100 不可省：浏览器对 disabled fieldset 默认 opacity:0，
 * 不加则非保存期整块消失。
 */
function DraftFieldSet({ saving, children }: { saving: boolean; children: ReactNode }) {
  return (
    <fieldset
      disabled={saving}
      className="m-0 min-w-0 border-0 p-0 transition-opacity duration-[var(--duration-base)] enabled:opacity-100 disabled:opacity-60 disabled:cursor-not-allowed"
    >
      {children}
    </fieldset>
  );
}

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
          {/* 两行的父容器负责关闭末行分隔线（FormRow 始终渲染 border-b）。
              该类不能加在卡片上：卡片里 FormFooter 是最后一个子节点，卡片上的 :last-child
              命中的是页脚，被去掉的会是页脚的下边框；FormFooter 必须仍是卡片的直接子节点且留在最后。 */}
          <div className="[&>*:last-child]:border-b-0">
            <FormRow
              label={copy.settings.appearance.themeTitle}
              description={copy.settings.appearance.themeDescription}
            >
              {/* 保存进行中禁用整块控件，避免在途编辑被静默丢弃（详见 DraftFieldSet 注释）。 */}
              <DraftFieldSet saving={saving}>
                <SegmentedControl
                  options={THEME_OPTIONS}
                  value={draft.theme}
                  onChange={(value) => set({ theme: value as ThemePreferenceValue })}
                  ariaLabel={copy.settings.appearance.themeAria}
                />
              </DraftFieldSet>
            </FormRow>

            <FormRow
              label={copy.settings.appearance.fontSizeTitle}
              description={copy.settings.appearance.fontSizeDescription}
            >
              <DraftFieldSet saving={saving}>
                <SegmentedControl
                  options={FONT_SIZE_OPTIONS}
                  value={draft.chat_font_size}
                  onChange={(value) => set({ chat_font_size: value as ChatFontSize })}
                  ariaLabel={copy.settings.appearance.fontSizeAria}
                />
              </DraftFieldSet>
            </FormRow>
          </div>

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
