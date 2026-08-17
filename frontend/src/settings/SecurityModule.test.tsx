import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/errors';
import type { AuthApi } from '../auth/api';
import { AuthProvider } from '../auth/AuthProvider';
import { createMemoryAuthHub } from '../auth/channel';
import { AuthSessionStore } from '../auth/session';
import type { DeviceSession, User } from '../auth/types';
import { copy } from '../copy';
import type { NotificationsStore } from '../notifications/store';
import type { ThemeController } from '../theme/theme';
import type { SettingsApi } from './api';
import { SecurityModule } from './SecurityModule';
import { SettingsProvider } from './SettingsProvider';

function testUser(): User {
  return {
    id: 'u_1',
    username: 'zhangsan',
    display_name: '张三',
    real_name: '张三',
    department: null,
    role: 'user',
    avatar_url: null,
  };
}

async function createAuthedStore(
  overrides: Partial<AuthApi> = {},
): Promise<{ store: AuthSessionStore; api: AuthApi }> {
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
  const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
  await store.login('zhangsan', 'password123');
  return { store, api };
}

function renderSecurity(store: AuthSessionStore, api: SettingsApi) {
  return render(
    <AuthProvider store={store}>
      <SettingsProvider
        api={Object.assign(
          { getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })) },
          api,
        ) as SettingsApi}
        authStore={store}
        theme={{ setPreference: vi.fn() } as unknown as ThemeController}
        notifications={{} as NotificationsStore}
      >
        <SecurityModule />
      </SettingsProvider>
    </AuthProvider>,
  );
}

async function enterPasswordChange(
  user: ReturnType<typeof userEvent.setup>,
  oldPassword = 'password123',
  newPassword = 'newpassword1',
): Promise<void> {
  await user.type(screen.getByLabelText(copy.settings.security.oldPasswordLabel), oldPassword);
  await user.type(screen.getByLabelText(copy.settings.security.newPasswordLabel), newPassword);
  await user.type(screen.getByLabelText(copy.settings.security.confirmPasswordLabel), newPassword);
  await user.click(screen.getByRole('button', { name: copy.settings.security.changePassword }));
}

const CURRENT_SESSION: DeviceSession = {
  id: 'sess_current',
  device: 'Current browser',
  last_active_at: '2026-08-01T00:00:00.000Z',
  current: true,
};

const OTHER_SESSION: DeviceSession = {
  id: 'sess_other',
  device: 'Other phone',
  last_active_at: '2026-08-02T00:00:00.000Z',
  current: false,
};

function serverError(): ApiError {
  return new ApiError({
    status: 500,
    code: 'internal_error',
    message: '',
    details: {},
    requestId: null,
  });
}

describe('SecurityModule', () => {
  it('loads sessions, marks the current device, and logs out the current device directly without a dialog', async () => {
    const { store, api: authApi } = await createAuthedStore({
      listSessions: vi.fn(async () => [CURRENT_SESSION]),
    });
    const logout = vi.spyOn(store, 'logout');
    const settingsApi = { changePassword: vi.fn(async () => {}) } as unknown as SettingsApi;
    const user = userEvent.setup();

    renderSecurity(store, settingsApi);

    expect(await screen.findByText(CURRENT_SESSION.device)).toBeInTheDocument();
    expect(screen.getByText(copy.settings.security.currentDevice)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: copy.settings.security.logoutCurrent }));

    await waitFor(() => expect(logout).toHaveBeenCalledOnce());
    expect(authApi.logout).toHaveBeenCalledOnce();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('revokes another device with the correct id and removes it from the list without a dialog', async () => {
    const { store, api: authApi } = await createAuthedStore({
      listSessions: vi.fn(async () => [CURRENT_SESSION, OTHER_SESSION]),
    });
    const revokeSession = vi.spyOn(store, 'revokeSession');
    const settingsApi = { changePassword: vi.fn(async () => {}) } as unknown as SettingsApi;
    const user = userEvent.setup();

    renderSecurity(store, settingsApi);
    expect(await screen.findByText(OTHER_SESSION.device)).toBeInTheDocument();
    expect(screen.getByText(CURRENT_SESSION.device)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: copy.settings.security.logoutOther }));

    await waitFor(() =>
      expect(revokeSession).toHaveBeenCalledWith(OTHER_SESSION.id, { current: false }),
    );
    expect(authApi.revokeSession).toHaveBeenCalledWith(OTHER_SESSION.id);
    await waitFor(() => expect(screen.queryByText(OTHER_SESSION.device)).not.toBeInTheDocument());
    expect(screen.getByText(CURRENT_SESSION.device)).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('keeps the other device listed and shows an accessible error when revoke fails', async () => {
    const { store } = await createAuthedStore({
      listSessions: vi.fn(async () => [CURRENT_SESSION, OTHER_SESSION]),
      revokeSession: vi.fn(async () => {
        throw serverError();
      }),
    });
    const settingsApi = { changePassword: vi.fn(async () => {}) } as unknown as SettingsApi;
    const user = userEvent.setup();

    renderSecurity(store, settingsApi);
    expect(await screen.findByText(OTHER_SESSION.device)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: copy.settings.security.logoutOther }));

    expect(await screen.findByRole('alert')).toHaveTextContent(copy.settings.security.sessionActionError);
    expect(screen.getByText(OTHER_SESSION.device)).toBeInTheDocument();
    expect(screen.getByText(CURRENT_SESSION.device)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: copy.settings.security.logoutOther })).toBeEnabled();
    expect(store.getState().status).toBe('authenticated');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('logs out all devices directly through the existing all-session store action without a dialog', async () => {
    const { store, api: authApi } = await createAuthedStore({
      listSessions: vi.fn(async () => [CURRENT_SESSION]),
    });
    const revokeAllSessions = vi.spyOn(store, 'revokeAllSessions');
    const settingsApi = { changePassword: vi.fn(async () => {}) } as unknown as SettingsApi;
    const user = userEvent.setup();

    renderSecurity(store, settingsApi);
    await screen.findByText(CURRENT_SESSION.device);
    await user.click(screen.getByRole('button', { name: copy.settings.security.logoutAll }));

    await waitFor(() => expect(revokeAllSessions).toHaveBeenCalledOnce());
    expect(authApi.revokeAllSessions).toHaveBeenCalledOnce();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('shows an accessible error and does not forge logout when revoke-all fails', async () => {
    const { store } = await createAuthedStore({
      listSessions: vi.fn(async () => [CURRENT_SESSION, OTHER_SESSION]),
      revokeAllSessions: vi.fn(async () => {
        throw serverError();
      }),
    });
    const settingsApi = { changePassword: vi.fn(async () => {}) } as unknown as SettingsApi;
    const user = userEvent.setup();

    renderSecurity(store, settingsApi);
    expect(await screen.findByText(CURRENT_SESSION.device)).toBeInTheDocument();
    expect(screen.getByText(OTHER_SESSION.device)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: copy.settings.security.logoutAll }));

    expect(await screen.findByRole('alert')).toHaveTextContent(copy.settings.security.sessionActionError);
    expect(store.getState().status).toBe('authenticated');
    expect(screen.getByText(CURRENT_SESSION.device)).toBeInTheDocument();
    expect(screen.getByText(OTHER_SESSION.device)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: copy.settings.security.logoutAll })).toBeEnabled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('rejects a locally invalid new password with the exact rule before sending a request', async () => {
    const { store } = await createAuthedStore();
    const changePassword = vi.fn(async () => {});
    const settingsApi = { changePassword } as unknown as SettingsApi;
    const user = userEvent.setup();

    renderSecurity(store, settingsApi);
    await enterPasswordChange(user, 'password123', 'letters');

    expect(changePassword).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent(copy.settings.security.invalidPasswordRule);
    expect(screen.getByLabelText(copy.settings.security.newPasswordLabel)).toHaveAttribute(
      'aria-invalid',
      'true',
    );
  });

  it.each([
    [
      new ApiError({
        status: 400,
        code: 'invalid_password_rule',
        message: '',
        details: {},
        requestId: null,
      }),
      copy.settings.security.invalidPasswordRule,
      copy.settings.security.newPasswordLabel,
    ],
    [
      new ApiError({
        status: 403,
        code: 'wrong_old_password',
        message: '',
        details: {},
        requestId: null,
      }),
      copy.settings.security.wrongOldPassword,
      copy.settings.security.oldPasswordLabel,
    ],
  ])('maps %s to its relevant field', async (error, message, fieldLabel) => {
    const { store } = await createAuthedStore();
    const settingsApi = {
      changePassword: vi.fn(async () => Promise.reject(error)),
    } as unknown as SettingsApi;
    const user = userEvent.setup();

    renderSecurity(store, settingsApi);
    await enterPasswordChange(user);

    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(screen.getByLabelText(fieldLabel)).toHaveAttribute('aria-invalid', 'true');
  });

  it('clears local and peer authentication after a successful password change without issuing DELETE /auth/sessions', async () => {
    const hub = createMemoryAuthHub();
    const authApi: AuthApi = {
      login: vi.fn(async () => ({ token: 'tok_login', user: testUser() })),
      logout: vi.fn(async () => {}),
      refresh: vi.fn(async () => ({ token: 'tok_refresh' })),
      me: vi.fn(async () => testUser()),
      listSessions: vi.fn(async () => []),
      revokeSession: vi.fn(async () => {}),
      revokeAllSessions: vi.fn(async () => {}),
    };
    const store = new AuthSessionStore({ api: authApi, bus: hub.createBus() });
    const peer = new AuthSessionStore({ api: authApi, bus: hub.createBus() });
    await store.login('zhangsan', 'password123');
    const handleServerAllSessionsRevoked = vi.spyOn(store, 'handleServerAllSessionsRevoked');
    const revokeAllSessions = vi.spyOn(store, 'revokeAllSessions');
    const settingsApi = { changePassword: vi.fn(async () => {}) } as unknown as SettingsApi;
    const user = userEvent.setup();

    renderSecurity(store, settingsApi);
    await enterPasswordChange(user);

    await waitFor(() => expect(handleServerAllSessionsRevoked).toHaveBeenCalledOnce());
    expect(revokeAllSessions).not.toHaveBeenCalled();
    expect(authApi.revokeAllSessions).not.toHaveBeenCalled();
    expect(store.getState().status).toBe('unauthenticated');
    expect(peer.getState().status).toBe('unauthenticated');
  });

  it('ignores a deferred account-A password success after logout and login as B in the same tab', async () => {
    const accountA = testUser();
    const accountB: User = {
      ...testUser(),
      id: 'u_2',
      username: 'lisi',
      display_name: '李四',
      real_name: '李四',
    };
    let resolvePassword!: () => void;
    const changePassword = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolvePassword = resolve;
        }),
    );
    let loginCalls = 0;
    const authApi: AuthApi = {
      login: vi.fn(async () => {
        loginCalls += 1;
        return loginCalls === 1
          ? { token: 'tok_account_a', user: accountA }
          : { token: 'tok_account_b', user: accountB };
      }),
      logout: vi.fn(async () => {}),
      refresh: vi.fn(async () => ({ token: 'tok_refresh' })),
      me: vi.fn(async () => accountB),
      listSessions: vi.fn(async () => []),
      revokeSession: vi.fn(async () => {}),
      revokeAllSessions: vi.fn(async () => {}),
    };
    const store = new AuthSessionStore({ api: authApi, bus: createMemoryAuthHub().createBus() });
    await store.login('zhangsan', 'password123');
    const settingsApi = { changePassword } as unknown as SettingsApi;
    const user = userEvent.setup();

    renderSecurity(store, settingsApi);
    await enterPasswordChange(user);
    await waitFor(() => expect(changePassword).toHaveBeenCalledOnce());

    await act(async () => {
      await store.logout();
      await store.login('lisi', 'password123');
    });
    expect(store.getState()).toEqual({ status: 'authenticated', token: 'tok_account_b', user: accountB });

    // Deferred password resolve finishes changePassword (finally setState); wrap in act so React sees it.
    await act(async () => {
      resolvePassword();
    });

    expect(store.getState()).toEqual({ status: 'authenticated', token: 'tok_account_b', user: accountB });
    expect(authApi.revokeAllSessions).not.toHaveBeenCalled();
  });

  it('ignores a deferred account-A password success after logout and re-login as a new A session', async () => {
    const accountA = testUser();
    let resolvePassword!: () => void;
    const changePassword = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolvePassword = resolve;
        }),
    );
    let loginCalls = 0;
    const authApi: AuthApi = {
      login: vi.fn(async () => {
        loginCalls += 1;
        return { token: `tok_account_a_${loginCalls}`, user: accountA };
      }),
      logout: vi.fn(async () => {}),
      refresh: vi.fn(async () => ({ token: 'tok_refresh' })),
      me: vi.fn(async () => accountA),
      listSessions: vi.fn(async () => []),
      revokeSession: vi.fn(async () => {}),
      revokeAllSessions: vi.fn(async () => {}),
    };
    const store = new AuthSessionStore({ api: authApi, bus: createMemoryAuthHub().createBus() });
    await store.login('zhangsan', 'password123');
    const settingsApi = { changePassword } as unknown as SettingsApi;
    const user = userEvent.setup();

    renderSecurity(store, settingsApi);
    await enterPasswordChange(user);
    await waitFor(() => expect(changePassword).toHaveBeenCalledOnce());

    await act(async () => {
      await store.logout();
      await store.login('zhangsan', 'password123');
    });
    expect(store.getState().status).toBe('authenticated');
    expect(store.getState().token).toBe('tok_account_a_2');

    await act(async () => {
      resolvePassword();
    });

    expect(store.getState().status).toBe('authenticated');
    expect(store.getState().token).toBe('tok_account_a_2');
    expect(store.getState().user).toEqual(accountA);
  });

  it('still clears the current logical session after a normal in-session refresh', async () => {
    const hub = createMemoryAuthHub();
    const authApi: AuthApi = {
      login: vi.fn(async () => ({ token: 'tok_login', user: testUser() })),
      logout: vi.fn(async () => {}),
      refresh: vi.fn(async () => ({ token: 'tok_refresh' })),
      me: vi.fn(async () => testUser()),
      listSessions: vi.fn(async () => []),
      revokeSession: vi.fn(async () => {}),
      revokeAllSessions: vi.fn(async () => {}),
    };
    const store = new AuthSessionStore({ api: authApi, bus: hub.createBus() });
    const peer = new AuthSessionStore({ api: authApi, bus: hub.createBus() });
    await store.login('zhangsan', 'password123');
    await store.refresh();
    expect(store.getState().token).toBe('tok_refresh');
    expect(peer.getState().token).toBe('tok_refresh');

    const settingsApi = { changePassword: vi.fn(async () => {}) } as unknown as SettingsApi;
    const user = userEvent.setup();
    renderSecurity(store, settingsApi);
    await enterPasswordChange(user);

    await waitFor(() => expect(store.getState().status).toBe('unauthenticated'));
    expect(peer.getState().status).toBe('unauthenticated');
    expect(authApi.revokeAllSessions).not.toHaveBeenCalled();
  });

  it('does not clear a peer that re-authenticated under a different logical session when a delayed all-sessions bus event arrives', async () => {
    const hub = createMemoryAuthHub();
    const accountA = testUser();
    const accountB: User = {
      ...testUser(),
      id: 'u_2',
      username: 'lisi',
      display_name: '李四',
      real_name: '李四',
    };
    let resolvePassword!: () => void;
    const changePassword = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolvePassword = resolve;
        }),
    );
    const authApiA: AuthApi = {
      login: vi.fn(async () => ({ token: 'tok_account_a', user: accountA })),
      logout: vi.fn(async () => {}),
      refresh: vi.fn(async () => ({ token: 'tok_refresh_a' })),
      me: vi.fn(async () => accountA),
      listSessions: vi.fn(async () => []),
      revokeSession: vi.fn(async () => {}),
      revokeAllSessions: vi.fn(async () => {}),
    };
    const authApiPeer: AuthApi = {
      login: vi.fn(async () => ({ token: 'tok_account_b', user: accountB })),
      logout: vi.fn(async () => {}),
      refresh: vi.fn(async () => ({ token: 'tok_refresh_b' })),
      me: vi.fn(async () => accountB),
      listSessions: vi.fn(async () => []),
      revokeSession: vi.fn(async () => {}),
      revokeAllSessions: vi.fn(async () => {}),
    };
    const store = new AuthSessionStore({ api: authApiA, bus: hub.createBus() });
    const peer = new AuthSessionStore({ api: authApiPeer, bus: hub.createBus() });
    await store.login('zhangsan', 'password123');
    expect(peer.getState().status).toBe('authenticated');
    const staleAuthSessionId = store.getAuthSessionId();
    expect(staleAuthSessionId).toBe('tok_account_a');

    const settingsApi = { changePassword } as unknown as SettingsApi;
    const user = userEvent.setup();
    renderSecurity(store, settingsApi);
    await enterPasswordChange(user);
    await waitFor(() => expect(changePassword).toHaveBeenCalledOnce());

    // Shared auth bus keeps tabs aligned: peer logout/login also moves the originator tab to B.
    await act(async () => {
      await peer.logout();
      await peer.login('lisi', 'password456');
    });
    expect(peer.getState()).toEqual({ status: 'authenticated', token: 'tok_account_b', user: accountB });
    expect(store.getState()).toEqual({ status: 'authenticated', token: 'tok_account_b', user: accountB });
    expect(store.getAuthSessionId()).not.toBe(staleAuthSessionId);
    expect(peer.getAuthSessionId()).not.toBe(staleAuthSessionId);

    await act(async () => {
      resolvePassword();
    });

    // Deferred A password success must not clear the new B logical session on either tab.
    expect(store.getState()).toEqual({ status: 'authenticated', token: 'tok_account_b', user: accountB });
    expect(peer.getState()).toEqual({ status: 'authenticated', token: 'tok_account_b', user: accountB });
    expect(authApiA.revokeAllSessions).not.toHaveBeenCalled();
    expect(authApiPeer.revokeAllSessions).not.toHaveBeenCalled();
  });
});

describe('SecurityModule 会话 fence（review Major 1：A 的会话列表不在 B 显示）', () => {
  it('旧会话的延迟 listSessions 响应不覆盖新会话列表', async () => {
    let resolveFirst!: (value: DeviceSession[]) => void;
    let resolveSecond!: (value: DeviceSession[]) => void;
    const listSessions = vi
      .fn<AuthApi['listSessions']>()
      .mockReturnValueOnce(new Promise<DeviceSession[]>((resolve) => (resolveFirst = resolve)))
      .mockReturnValueOnce(new Promise<DeviceSession[]>((resolve) => (resolveSecond = resolve)));
    // 同一账号两次 login 返回不同 token → 不同 authSessionId（真实会话切换）
    let loginCount = 0;
    const login = vi.fn(async () => {
      loginCount += 1;
      return { token: loginCount === 1 ? 'tok_session_a' : 'tok_session_b', user: testUser() };
    });
    const { store } = await createAuthedStore({ listSessions, login });
    const settingsApi = { changePassword: vi.fn(async () => {}) } as unknown as SettingsApi;

    renderSecurity(store, settingsApi);
    // 等待首次请求发出
    await waitFor(() => expect(listSessions).toHaveBeenCalledTimes(1));

    // 会话切换（同一账号重新 login 生成新 authSessionId）
    await act(async () => {
      await store.login('zhangsan', 'password123');
    });
    await waitFor(() => expect(listSessions).toHaveBeenCalledTimes(2));

    // 新会话响应先到：显示 B 的会话
    await act(async () => {
      resolveSecond([
        { id: 'sess_b', device: 'B 的浏览器', last_active_at: '2026-08-03T00:00:00.000Z', current: true },
      ]);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(await screen.findByText('B 的浏览器')).toBeInTheDocument();

    // 旧会话（A）响应迟到：不得覆盖 B 的列表
    await act(async () => {
      resolveFirst([
        { id: 'sess_a', device: 'A 的浏览器', last_active_at: '2026-08-01T00:00:00.000Z', current: true },
      ]);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(screen.queryByText('A 的浏览器')).not.toBeInTheDocument();
    expect(screen.getByText('B 的浏览器')).toBeInTheDocument();
  });
});
