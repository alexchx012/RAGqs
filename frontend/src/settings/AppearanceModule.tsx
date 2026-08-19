import { copy } from '../copy';
import { SegmentedControl, type SegmentedOption } from '../ui/SegmentedControl';
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

  const selectTheme = (value: string) => {
    if (preferences === null || saving) {
      return;
    }
    const themePreference = value as ThemePreferenceValue;
    if (themePreference === preferences.theme) {
      return;
    }
    save({ ...preferences, theme: themePreference });
  };

  const selectFontSize = (value: string) => {
    if (preferences === null || saving) {
      return;
    }
    const chatFontSize = value as ChatFontSize;
    if (chatFontSize === preferences.chat_font_size) {
      return;
    }
    save({ ...preferences, chat_font_size: chatFontSize });
  };

  return (
    <section
      aria-label={copy.settings.appearance.sectionLabel}
      aria-busy={loading || saving}
      className="pb-10"
    >
      {loading && (
        <p role="status" className="text-caption text-smoke-gray">
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

      {!loading && !loadError && preferences !== null && (
        <>
          <section aria-labelledby="settings-appearance-theme" className="border-b border-hairline pb-8">
            <h2 id="settings-appearance-theme" className="text-subheading font-medium text-ink-black">
              {copy.settings.appearance.themeTitle}
            </h2>
            <p className="mt-2 text-caption text-smoke-gray">{copy.settings.appearance.themeDescription}</p>
            <fieldset disabled={saving} className="mt-4 min-w-0 border-0 p-0">
              <SegmentedControl
                options={THEME_OPTIONS}
                value={preferences.theme}
                onChange={selectTheme}
                ariaLabel={copy.settings.appearance.themeAria}
              />
            </fieldset>
          </section>

          <section aria-labelledby="settings-appearance-font-size" className="py-8">
            <h2 id="settings-appearance-font-size" className="text-subheading font-medium text-ink-black">
              {copy.settings.appearance.fontSizeTitle}
            </h2>
            <p className="mt-2 text-caption text-smoke-gray">{copy.settings.appearance.fontSizeDescription}</p>
            <fieldset disabled={saving} className="mt-4 min-w-0 border-0 p-0">
              <SegmentedControl
                options={FONT_SIZE_OPTIONS}
                value={preferences.chat_font_size}
                onChange={selectFontSize}
                ariaLabel={copy.settings.appearance.fontSizeAria}
              />
            </fieldset>
          </section>

          {saveError && (
            <p role="alert" className="text-caption text-danger">
              {copy.settings.appearance.saveError}
            </p>
          )}
        </>
      )}
    </section>
  );
}
