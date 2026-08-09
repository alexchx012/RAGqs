import { useCallback, useEffect, useRef, useState } from 'react';
import { useAuthState, useAuthStore } from '../auth/AuthProvider';
import { copy } from '../copy';
import { SegmentedControl, type SegmentedOption } from '../ui/SegmentedControl';
import { Switch } from '../ui/Switch';
import { useSettings, type AppearanceSyncHandle } from './SettingsProvider';
import type { ChatFontSize, ThemePreferenceValue, UserPreferences } from './types';

type PreferenceControl = 'theme' | 'chat_font_size' | 'ab_opt_out';

const THEME_OPTIONS: SegmentedOption[] = [
  { value: 'light', label: copy.settings.appearance.themeLight },
  { value: 'dark', label: copy.settings.appearance.themeDark },
  { value: 'system', label: copy.settings.appearance.themeSystem },
];

const FONT_SIZE_OPTIONS: SegmentedOption[] = [
  { value: 'standard', label: copy.settings.appearance.fontStandard },
  { value: 'large', label: copy.settings.appearance.fontLarge },
];

function getSessionKey(status: ReturnType<typeof useAuthState>['status'], userId: string | null, authSessionId: string | null): string | null {
  if (status !== 'authenticated' || userId === null || authSessionId === null) {
    return null;
  }
  return `${authSessionId}:${userId}`;
}

export function AppearanceModule() {
  const { beginAppearanceSync } = useSettings();
  const authState = useAuthState();
  const authStore = useAuthStore();
  const authSessionId = authStore.getAuthSessionId();
  const sessionKey = getSessionKey(authState.status, authState.user?.id ?? null, authSessionId);
  const [preferences, setPreferences] = useState<UserPreferences | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [saveError, setSaveError] = useState(false);
  const [saving, setSaving] = useState<PreferenceControl | null>(null);
  const requestSequenceRef = useRef(0);
  const mountedRef = useRef(false);
  const committedPreferencesRef = useRef<UserPreferences | null>(null);

  const isCurrentOperation = useCallback(
    (sequence: number, sync: AppearanceSyncHandle): boolean =>
      mountedRef.current && requestSequenceRef.current === sequence && sync.isCurrent(),
    [],
  );

  const loadPreferences = useCallback(async () => {
    const sequence = ++requestSequenceRef.current;
    const sync = beginAppearanceSync('load');
    committedPreferencesRef.current = null;
    setLoading(true);
    setLoadError(false);
    setSaveError(false);
    setSaving(null);

    if (sessionKey === null || !sync.isCurrent()) {
      setLoading(false);
      return;
    }

    try {
      const loaded = await sync.load();
      if (!isCurrentOperation(sequence, sync)) {
        return;
      }
      committedPreferencesRef.current = loaded;
      setPreferences(loaded);
      sync.apply(loaded);
    } catch {
      if (!isCurrentOperation(sequence, sync)) {
        return;
      }
      setLoadError(true);
    } finally {
      if (isCurrentOperation(sequence, sync)) {
        setLoading(false);
      }
    }
  }, [beginAppearanceSync, isCurrentOperation, sessionKey]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      requestSequenceRef.current += 1;
    };
  }, []);

  useEffect(() => {
    if (sessionKey === null) {
      ++requestSequenceRef.current;
      committedPreferencesRef.current = null;
      setPreferences(null);
      setLoading(false);
      setLoadError(false);
      setSaveError(false);
      setSaving(null);
      return;
    }
    void loadPreferences();
  }, [loadPreferences, sessionKey]);

  const savePreferences = useCallback(
    async (next: UserPreferences, control: PreferenceControl) => {
      const previous = committedPreferencesRef.current;
      if (previous === null || sessionKey === null) {
        return;
      }
      const sequence = ++requestSequenceRef.current;
      const sync = beginAppearanceSync('save');
      if (!sync.isCurrent()) {
        return;
      }
      setSaving(control);
      setSaveError(false);
      setPreferences(next);
      sync.apply(next);

      try {
        const saved = await sync.save(next, previous);
        if (!sync.isCurrent()) {
          return;
        }
        // The component may have unmounted after the drawer closed. A current-session
        // terminal response still owns the shared runtime, but never updates React state.
        sync.apply(saved);
        if (!isCurrentOperation(sequence, sync)) {
          return;
        }
        committedPreferencesRef.current = saved;
        setPreferences(saved);
      } catch {
        if (!sync.isCurrent()) {
          return;
        }
        // A current-session failure must also rollback after unmount; only the mounted
        // component receives the visible error and local snapshot update.
        sync.apply(previous);
        if (!isCurrentOperation(sequence, sync)) {
          return;
        }
        committedPreferencesRef.current = previous;
        setPreferences(previous);
        setSaveError(true);
      } finally {
        if (isCurrentOperation(sequence, sync)) {
          setSaving(null);
        }
      }
    },
    [beginAppearanceSync, isCurrentOperation, sessionKey],
  );

  const selectTheme = (value: string) => {
    if (preferences === null || saving !== null) {
      return;
    }
    const themePreference = value as ThemePreferenceValue;
    if (themePreference === preferences.theme) {
      return;
    }
    void savePreferences({ ...preferences, theme: themePreference }, 'theme');
  };

  const selectFontSize = (value: string) => {
    if (preferences === null || saving !== null) {
      return;
    }
    const chatFontSize = value as ChatFontSize;
    if (chatFontSize === preferences.chat_font_size) {
      return;
    }
    void savePreferences({ ...preferences, chat_font_size: chatFontSize }, 'chat_font_size');
  };

  const toggleAbOptOut = (checked: boolean) => {
    if (preferences === null || saving !== null || checked === preferences.ab_opt_out) {
      return;
    }
    void savePreferences({ ...preferences, ab_opt_out: checked }, 'ab_opt_out');
  };

  return (
    <section
      aria-label={copy.settings.appearance.sectionLabel}
      aria-busy={loading || saving !== null}
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
            onClick={() => void loadPreferences()}
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
            <fieldset disabled={saving !== null} className="mt-4 min-w-0 border-0 p-0">
              <SegmentedControl
                options={THEME_OPTIONS}
                value={preferences.theme}
                onChange={selectTheme}
                ariaLabel={copy.settings.appearance.themeAria}
              />
            </fieldset>
          </section>

          <section aria-labelledby="settings-appearance-font-size" className="border-b border-hairline py-8">
            <h2 id="settings-appearance-font-size" className="text-subheading font-medium text-ink-black">
              {copy.settings.appearance.fontSizeTitle}
            </h2>
            <p className="mt-2 text-caption text-smoke-gray">{copy.settings.appearance.fontSizeDescription}</p>
            <fieldset disabled={saving !== null} className="mt-4 min-w-0 border-0 p-0">
              <SegmentedControl
                options={FONT_SIZE_OPTIONS}
                value={preferences.chat_font_size}
                onChange={selectFontSize}
                ariaLabel={copy.settings.appearance.fontSizeAria}
              />
            </fieldset>
          </section>

          <section aria-labelledby="settings-appearance-privacy" className="py-8">
            <h2 id="settings-appearance-privacy" className="text-subheading font-medium text-ink-black">
              {copy.settings.appearance.privacyTitle}
            </h2>
            <div className="mt-4 flex items-start justify-between gap-6">
              <div>
                <p className="text-body text-ink-black">{copy.settings.appearance.abOptOutLabel}</p>
                <p className="mt-2 max-w-[520px] text-caption text-smoke-gray">
                  {copy.settings.appearance.abOptOutDescription}
                </p>
              </div>
              <Switch
                checked={preferences.ab_opt_out}
                onCheckedChange={toggleAbOptOut}
                disabled={saving !== null}
                ariaLabel={copy.settings.appearance.abOptOutLabel}
              />
            </div>
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
