import { act, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AuthApi } from '../auth/api';
import { createMemoryAuthHub } from '../auth/channel';
import { AuthSessionStore } from '../auth/session';
import type { User } from '../auth/types';
import type { NotificationsStore } from '../notifications/store';
import type { ThemeController } from '../theme/theme';
import type { SettingsApi } from './api';
import { SettingsProvider, type SettingsContextValue, useSettings } from './SettingsProvider';

function testUser(overrides: Partial<User> = {}): User {
  return {
    id: 'u_1',
    username: 'zhangsan',
    display_name: '张三',
    real_name: '张三',
    department: null,
    role: 'user',
    avatar_url: null,
    ...overrides,
  };
}

function createAuthStore(overrides: Partial<AuthApi> = {}): AuthSessionStore {
  const api: AuthApi = {
    login: vi.fn(async () => ({ token: 'tok_login', user: testUser() })),
    logout: vi.fn(async () => {}),
    refresh: vi.fn(async () => ({ token: 'tok_refresh' })),
    me: vi.fn(async () => testUser()),
    listSessions: vi.fn(async () => []),
    revokeSession: vi.fn(async () => {}),
    revokeAllSessions: vi.fn(async () => {}),
    ...overrides,
  };
  return new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
}

function settingsDependencies(authStore: AuthSessionStore) {
  return {
    api: {
      getPreferences: vi.fn(async () => ({
        theme: 'system',
        chat_font_size: 'standard',
        ab_opt_out: false,
      })),
    } as unknown as SettingsApi,
    authStore,
    theme: { setPreference: vi.fn() } as unknown as ThemeController,
    notifications: {} as NotificationsStore,
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

function SyncHandleCapture({
  onReady,
}: {
  readonly onReady: (begin: SettingsContextValue['beginCurrentUserPresentationSync']) => void;
}) {
  onReady(useSettings().beginCurrentUserPresentationSync);
  return null;
}

function ContextBoundaryProbe() {
  const settings = useSettings();
  return (
    <output data-testid="context-boundary">
      {`${'authStore' in settings ? 'leaked' : 'narrow'}:${typeof settings.beginCurrentUserPresentationSync}`}
    </output>
  );
}

afterEach(() => {
  delete document.documentElement.dataset.chatFontSize;
});

describe('SettingsProvider 当前用户展示快照边界', () => {
  it('不同展示字段的乱序保存互不废弃', async () => {
    const store = createAuthStore();
    await store.login('zhangsan', 'password123');
    let begin: SettingsContextValue['beginCurrentUserPresentationSync'] | null = null;

    render(
      <SettingsProvider {...settingsDependencies(store)}>
        <SyncHandleCapture onReady={(next) => (begin = next)} />
      </SettingsProvider>,
    );
    if (begin === null) {
      throw new Error('presentation sync handle factory was not provided');
    }
    const beginPresentationSync = begin as SettingsContextValue['beginCurrentUserPresentationSync'];

    const displayNameSave = beginPresentationSync(['display_name']);
    const avatarSave = beginPresentationSync(['avatar_url']);
    act(() => {
      avatarSave.commit({ avatar_url: '/avatars/second.png' });
      displayNameSave.commit({ display_name: '较早请求的显示名' });
    });

    expect(store.getState().user).toEqual({
      ...testUser(),
      display_name: '较早请求的显示名',
      avatar_url: '/avatars/second.png',
    });
  });

  it('同一展示字段只接受最后启动的保存结果', async () => {
    const store = createAuthStore();
    await store.login('zhangsan', 'password123');
    let begin: SettingsContextValue['beginCurrentUserPresentationSync'] | null = null;

    render(
      <SettingsProvider {...settingsDependencies(store)}>
        <SyncHandleCapture onReady={(next) => (begin = next)} />
      </SettingsProvider>,
    );
    if (begin === null) {
      throw new Error('presentation sync handle factory was not provided');
    }
    const beginPresentationSync = begin as SettingsContextValue['beginCurrentUserPresentationSync'];

    const firstDisplayNameSave = beginPresentationSync(['display_name']);
    const secondDisplayNameSave = beginPresentationSync(['display_name']);
    act(() => {
      secondDisplayNameSave.commit({ display_name: '第二次保存' });
      firstDisplayNameSave.commit({ display_name: '第一次迟到保存' });
    });

    expect(store.getState().user).toEqual({
      ...testUser(),
      display_name: '第二次保存',
    });
  });

  it('未认证时 provider handle 不创建用户', async () => {
    const store = createAuthStore();
    await store.logout();
    let begin: SettingsContextValue['beginCurrentUserPresentationSync'] | null = null;

    render(
      <SettingsProvider {...settingsDependencies(store)}>
        <SyncHandleCapture onReady={(next) => (begin = next)} />
      </SettingsProvider>,
    );
    if (begin === null) {
      throw new Error('presentation sync handle factory was not provided');
    }
    const beginPresentationSync = begin as SettingsContextValue['beginCurrentUserPresentationSync'];

    beginPresentationSync(['display_name', 'avatar_url']).commit({
      display_name: '不应写入',
      avatar_url: '/avatars/no-user.png',
    });

    expect(store.getState()).toEqual({ status: 'unauthenticated', token: null, user: null });
  });

  it('不向设置模块暴露完整 AuthSessionStore，只暴露受控 handle 工厂', () => {
    const store = createAuthStore();

    render(
      <SettingsProvider {...settingsDependencies(store)}>
        <ContextBoundaryProbe />
      </SettingsProvider>,
    );

    expect(screen.getByTestId('context-boundary')).toHaveTextContent('narrow:function');
  });

  it('在认证会话建立时 hydrate runtime 外观，并在退出时恢复默认显示', async () => {
    const store = createAuthStore();
    const getPreferences = vi.fn(async () => ({
      theme: 'dark' as const,
      chat_font_size: 'large' as const,
      ab_opt_out: false,
    }));
    const setPreference = vi.fn();

    render(
      <SettingsProvider
        api={{ getPreferences } as unknown as SettingsApi}
        authStore={store}
        theme={{ setPreference } as unknown as ThemeController}
        notifications={{} as NotificationsStore}
      >
        <div />
      </SettingsProvider>,
    );

    await act(async () => {
      await store.login('zhangsan', 'password123');
    });
    await waitFor(() => expect(setPreference).toHaveBeenLastCalledWith('dark'));
    expect(document.documentElement.dataset.chatFontSize).toBe('large');
    expect(getPreferences).toHaveBeenCalledTimes(1);

    await act(async () => {
      await store.logout();
    });
    await waitFor(() => expect(setPreference).toHaveBeenLastCalledWith('system'));
    expect(document.documentElement.dataset.chatFontSize).toBeUndefined();
  });

  it('ignores an older session hydration response after a new logical session starts', async () => {
    const first = deferred<{ theme: 'light'; chat_font_size: 'standard'; ab_opt_out: boolean }>();
    const second = deferred<{ theme: 'dark'; chat_font_size: 'large'; ab_opt_out: boolean }>();
    const getPreferences = vi
      .fn<SettingsApi['getPreferences']>()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const setPreference = vi.fn();
    const store = createAuthStore({
      login: vi.fn(async (username) => ({
        token: username === 'first' ? 'tok_first' : 'tok_second',
        user: testUser({
          id: username === 'first' ? 'u_first' : 'u_second',
          username,
          display_name: username,
        }),
      })),
    });

    render(
      <SettingsProvider
        api={{ getPreferences } as unknown as SettingsApi}
        authStore={store}
        theme={{ setPreference } as unknown as ThemeController}
        notifications={{} as NotificationsStore}
      >
        <div />
      </SettingsProvider>,
    );

    await act(async () => {
      await store.login('first', 'password123');
    });
    await waitFor(() => expect(getPreferences).toHaveBeenCalledTimes(1));

    await act(async () => {
      await store.login('second', 'password123');
    });
    await waitFor(() => expect(getPreferences).toHaveBeenCalledTimes(2));

    await act(async () => {
      second.resolve({ theme: 'dark', chat_font_size: 'large', ab_opt_out: false });
      await second.promise;
    });
    await waitFor(() => expect(setPreference).toHaveBeenLastCalledWith('dark'));
    expect(document.documentElement.dataset.chatFontSize).toBe('large');

    await act(async () => {
      first.resolve({ theme: 'light', chat_font_size: 'standard', ab_opt_out: false });
      await first.promise;
    });
    expect(setPreference).toHaveBeenLastCalledWith('dark');
    expect(document.documentElement.dataset.chatFontSize).toBe('large');
  });
});
