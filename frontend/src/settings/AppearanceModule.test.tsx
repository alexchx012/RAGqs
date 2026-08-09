import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { copy } from '../copy';
import { createAuthedStore, renderWithSettings, testUser } from '../test/auth-fixtures';
import { ThemeController } from '../theme/theme';
import type { ThemeMedia, ThemeTarget } from '../theme/theme';
import type { SettingsApi } from './api';
import { AppearanceModule } from './AppearanceModule';
import type { UserPreferences } from './types';

const controllers = new Set<ThemeController>();

function createThemeController(systemDark = false): ThemeController {
  const listeners = new Set<(event: { matches: boolean }) => void>();
  const media: ThemeMedia = {
    matches: systemDark,
    addEventListener: (_type, listener) => {
      listeners.add(listener);
    },
    removeEventListener: (_type, listener) => {
      listeners.delete(listener);
    },
  };
  const classes = new Set<string>();
  const target: ThemeTarget = {
    dataset: {},
    classList: {
      add: (...tokens: string[]) => tokens.forEach((token) => classes.add(token)),
      remove: (...tokens: string[]) => tokens.forEach((token) => classes.delete(token)),
    },
    style: { colorScheme: '' },
  };
  const controller = new ThemeController(target, media);
  controllers.add(controller);
  return controller;
}

function preferences(overrides: Partial<UserPreferences> = {}): UserPreferences {
  return {
    theme: 'system',
    chat_font_size: 'standard',
    ab_opt_out: false,
    ...overrides,
  };
}

function createPreferencesApi(initial: UserPreferences) {
  const getPreferences = vi.fn(async () => initial);
  const updatePreferences = vi.fn(async (next: UserPreferences) => next);
  return {
    api: { getPreferences, updatePreferences } as unknown as SettingsApi,
    getPreferences,
    updatePreferences,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

async function renderAppearance(api: SettingsApi, theme: ThemeController) {
  const store = await createAuthedStore(testUser());
  let result!: ReturnType<typeof renderWithSettings>;
  await act(async () => {
    result = renderWithSettings(<AppearanceModule />, store, { api, theme });
    await Promise.resolve();
  });
  return { store, result };
}

afterEach(() => {
  for (const controller of controllers) {
    controller.dispose();
  }
  controllers.clear();
  delete document.documentElement.dataset.chatFontSize;
});

describe('AppearanceModule', () => {
  it('loads preferences and applies theme, chat font size, and privacy state', async () => {
    const initial = preferences({ theme: 'dark', chat_font_size: 'large', ab_opt_out: true });
    const { api } = createPreferencesApi(initial);
    const theme = createThemeController();

    await renderAppearance(api, theme);

    expect(await screen.findByRole('radio', { name: copy.settings.appearance.themeDark })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(theme.getPreference()).toBe('dark');
    expect(document.documentElement.dataset.chatFontSize).toBe('large');
    expect(screen.getByRole('radio', { name: copy.settings.appearance.fontLarge })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(screen.getByRole('switch', { name: copy.settings.appearance.abOptOutLabel })).toHaveAttribute(
      'data-state',
      'checked',
    );
  });

  it('persists every selection as a complete snapshot and applies it immediately', async () => {
    const initial = preferences();
    const { api, updatePreferences } = createPreferencesApi(initial);
    const theme = createThemeController();
    const user = userEvent.setup();

    await renderAppearance(api, theme);
    await screen.findByRole('radio', { name: copy.settings.appearance.themeSystem });

    await user.click(screen.getByRole('radio', { name: copy.settings.appearance.themeDark }));
    await waitFor(() =>
      expect(updatePreferences).toHaveBeenLastCalledWith({
        theme: 'dark',
        chat_font_size: 'standard',
        ab_opt_out: false,
      }),
    );
    expect(theme.getPreference()).toBe('dark');

    await user.click(screen.getByRole('radio', { name: copy.settings.appearance.fontLarge }));
    await waitFor(() =>
      expect(updatePreferences).toHaveBeenLastCalledWith({
        theme: 'dark',
        chat_font_size: 'large',
        ab_opt_out: false,
      }),
    );
    expect(document.documentElement.dataset.chatFontSize).toBe('large');

    await user.click(screen.getByRole('switch', { name: copy.settings.appearance.abOptOutLabel }));
    await waitFor(() =>
      expect(updatePreferences).toHaveBeenLastCalledWith({
        theme: 'dark',
        chat_font_size: 'large',
        ab_opt_out: true,
      }),
    );
  });

  it('rolls back the optimistic selection and shows an accessible error when saving fails', async () => {
    const initial = preferences();
    const { api, updatePreferences } = createPreferencesApi(initial);
    updatePreferences.mockRejectedValueOnce(new Error('offline'));
    const theme = createThemeController();
    const user = userEvent.setup();

    await renderAppearance(api, theme);
    await screen.findByRole('radio', { name: copy.settings.appearance.themeSystem });
    await user.click(screen.getByRole('radio', { name: copy.settings.appearance.themeDark }));

    expect(await screen.findByRole('alert')).toHaveTextContent(copy.settings.appearance.saveError);
    expect(theme.getPreference()).toBe('system');
    expect(document.documentElement.dataset.chatFontSize).toBe('standard');
    expect(screen.getByRole('radio', { name: copy.settings.appearance.themeSystem })).toHaveAttribute(
      'aria-checked',
      'true',
    );
  });

  it('shows a load error and retries without inventing a saved preference', async () => {
    const initial = preferences({ theme: 'light' });
    const { api, getPreferences } = createPreferencesApi(initial);
    getPreferences.mockRejectedValueOnce(new Error('offline'));
    const theme = createThemeController(true);
    const user = userEvent.setup();

    await renderAppearance(api, theme);

    expect(await screen.findByRole('alert')).toHaveTextContent(copy.settings.appearance.loadError);
    expect(screen.getByRole('button', { name: copy.settings.appearance.retry })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: copy.settings.appearance.retry }));

    expect(await screen.findByRole('radio', { name: copy.settings.appearance.themeLight })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(theme.getPreference()).toBe('light');
    expect(getPreferences).toHaveBeenCalledTimes(2);
  });

  it('keeps the new session snapshot when an older initial GET resolves afterward', async () => {
    const firstLoad = deferred<UserPreferences>();
    const secondLoad = deferred<UserPreferences>();
    const getPreferences = vi
      .fn<SettingsApi['getPreferences']>()
      .mockReturnValueOnce(firstLoad.promise)
      .mockReturnValueOnce(secondLoad.promise);
    const api = {
      getPreferences,
      updatePreferences: vi.fn(async (next: UserPreferences) => next),
    } as unknown as SettingsApi;
    const theme = createThemeController();

    const { store } = await renderAppearance(api, theme);
    await waitFor(() => expect(getPreferences).toHaveBeenCalledTimes(1));

    await act(async () => {
      await store.login('zhangsan', 'password123');
    });
    await waitFor(() => expect(getPreferences).toHaveBeenCalledTimes(2));

    const current = preferences({ theme: 'light', chat_font_size: 'large' });
    await act(async () => {
      secondLoad.resolve(current);
      await secondLoad.promise;
    });
    expect(await screen.findByRole('radio', { name: copy.settings.appearance.themeLight })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(theme.getPreference()).toBe('light');
    expect(document.documentElement.dataset.chatFontSize).toBe('large');

    await act(async () => {
      firstLoad.resolve(preferences({ theme: 'dark', chat_font_size: 'standard' }));
      await firstLoad.promise;
    });
    expect(screen.getByRole('radio', { name: copy.settings.appearance.themeLight })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(theme.getPreference()).toBe('light');
    expect(document.documentElement.dataset.chatFontSize).toBe('large');
  });

  it('ignores a prior-session save failure after the authentication session changes', async () => {
    const initial = preferences();
    const nextSession = preferences({ theme: 'light', chat_font_size: 'large' });
    const save = deferred<UserPreferences>();
    const getPreferences = vi
      .fn<SettingsApi['getPreferences']>()
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(nextSession);
    const updatePreferences = vi.fn<SettingsApi['updatePreferences']>(() => save.promise);
    const api = { getPreferences, updatePreferences } as unknown as SettingsApi;
    const theme = createThemeController();
    const user = userEvent.setup();

    const { store } = await renderAppearance(api, theme);
    await screen.findByRole('radio', { name: copy.settings.appearance.themeSystem });
    await user.click(screen.getByRole('radio', { name: copy.settings.appearance.themeDark }));
    await waitFor(() => expect(updatePreferences).toHaveBeenCalledTimes(1));
    expect(theme.getPreference()).toBe('dark');

    await act(async () => {
      await store.login('zhangsan', 'password123');
    });
    expect(await screen.findByRole('radio', { name: copy.settings.appearance.themeLight })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(theme.getPreference()).toBe('light');
    expect(document.documentElement.dataset.chatFontSize).toBe('large');

    await act(async () => {
      save.reject(new Error('offline'));
      await save.promise.catch(() => undefined);
    });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(theme.getPreference()).toBe('light');
    expect(document.documentElement.dataset.chatFontSize).toBe('large');
  });

  it('keeps the optimistic runtime until a pending save settles after unmount', async () => {
    const initial = preferences();
    const save = deferred<UserPreferences>();
    const getPreferences = vi.fn(async () => initial);
    const updatePreferences = vi.fn<SettingsApi['updatePreferences']>(() => save.promise);
    const api = { getPreferences, updatePreferences } as unknown as SettingsApi;
    const theme = createThemeController();
    const user = userEvent.setup();

    const { result } = await renderAppearance(api, theme);
    await screen.findByRole('radio', { name: copy.settings.appearance.themeSystem });
    await user.click(screen.getByRole('radio', { name: copy.settings.appearance.themeDark }));
    await waitFor(() => expect(updatePreferences).toHaveBeenCalledTimes(1));
    expect(theme.getPreference()).toBe('dark');

    result.unmount();
    expect(theme.getPreference()).toBe('dark');
    expect(document.documentElement.dataset.chatFontSize).toBe('standard');

    await act(async () => {
      save.resolve(preferences({ theme: 'dark' }));
      await save.promise;
    });
    expect(theme.getPreference()).toBe('dark');
    expect(document.documentElement.dataset.chatFontSize).toBe('standard');
  });

  it('rolls back the runtime when a pending save rejects after unmount', async () => {
    const initial = preferences();
    const save = deferred<UserPreferences>();
    const getPreferences = vi.fn(async () => initial);
    const updatePreferences = vi.fn<SettingsApi['updatePreferences']>(() => save.promise);
    const api = { getPreferences, updatePreferences } as unknown as SettingsApi;
    const theme = createThemeController();
    const user = userEvent.setup();

    const { result } = await renderAppearance(api, theme);
    await screen.findByRole('radio', { name: copy.settings.appearance.themeSystem });
    await user.click(screen.getByRole('radio', { name: copy.settings.appearance.themeDark }));
    await waitFor(() => expect(updatePreferences).toHaveBeenCalledTimes(1));
    result.unmount();

    await act(async () => {
      save.reject(new Error('offline'));
      await save.promise.catch(() => undefined);
    });
    expect(theme.getPreference()).toBe('system');
    expect(document.documentElement.dataset.chatFontSize).toBe('standard');
  });
});
