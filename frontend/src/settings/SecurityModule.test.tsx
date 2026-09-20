import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/errors';
import type { AuthApi } from '../auth/api';
import { AuthProvider } from '../auth/AuthProvider';
import { createMemoryAuthHub } from '../auth/channel';
import { AuthSessionStore } from '../auth/session';
import type { DeviceSession, User } from '../auth/types';
import { copy } from '../copy';
import { EscStackProvider } from '../lib/esc-stack-provider';
import type { NotificationsStore } from '../notifications/store';
import type { ThemeController } from '../theme/theme';
import type { SettingsApi } from './api';
import { SecurityModule } from './SecurityModule';
import { SettingsProvider } from './SettingsProvider';
import type { UserPreferences } from './types';

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
    // A38：退出全部设备的 ConfirmDialog 依赖 EscStackProvider（useEscShield）
    <EscStackProvider>
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
      </AuthProvider>
    </EscStackProvider>,
  );
}

/** 填写三个密码框并点卡片页脚「保存」提交改密（R13 后可见提交入口只剩页脚那一个）。 */
async function enterPasswordChange(
  user: ReturnType<typeof userEvent.setup>,
  oldPassword = 'password123',
  newPassword = 'newpassword1',
): Promise<void> {
  await user.type(screen.getByLabelText(copy.settings.security.oldPasswordLabel), oldPassword);
  await user.type(screen.getByLabelText(copy.settings.security.newPasswordLabel), newPassword);
  await user.type(screen.getByLabelText(copy.settings.security.confirmPasswordLabel), newPassword);
  await user.click(screen.getByRole('button', { name: copy.controls.save }));
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

/** 默认偏好快照：主题跟随系统、隐私开关关闭。 */
const PREFERENCES: UserPreferences = {
  theme: 'system',
  chat_font_size: 'standard',
  ab_opt_out: false,
};

/*
 * 保存回声必须还原真实 API 的对象身份行为：api/client.ts 的 `response.json()` 每次返回
 * **新对象**。若 mock 直接回声传入引用，useDraftForm 就观察不到 submitted 变化，会掩盖
 * 「保存落地即重置草稿」这类缺陷（Task 8 教训）。故统一经本函数：取值相同、身份必新。
 */
function echoPreferences(next: UserPreferences): UserPreferences {
  return { ...next };
}

const saveButton = () => screen.getByRole('button', { name: copy.controls.save });
const cancelButton = () => screen.getByRole('button', { name: copy.controls.cancel });
const privacySwitch = () =>
  screen.getByRole('switch', { name: copy.settings.security.abOptOutLabel });

interface ModuleOptions {
  /** 覆盖 SettingsApi 方法（getPreferences / updatePreferences / changePassword…）。 */
  readonly settings?: Record<string, unknown>;
  /** 覆盖 AuthApi（listSessions / revokeSession / revokeAllSessions…）。 */
  readonly auth?: Partial<AuthApi>;
  /** 默认等待隐私草稿就绪（开关出现）；加载态/错误态用例传 false。 */
  readonly waitForDraft?: boolean;
}

/** 设置模块默认装配：已认证 store + 偏好快照 + 可覆盖的 API。 */
async function renderModule(options: ModuleOptions = {}) {
  const { store } = await createAuthedStore(options.auth ?? {});
  const getPreferences = vi.fn(async () => PREFERENCES);
  const updatePreferences = vi.fn(async (next: UserPreferences) => echoPreferences(next));
  const changePassword = vi.fn(async () => {});
  const api = {
    getPreferences,
    updatePreferences,
    changePassword,
    ...(options.settings ?? {}),
  } as unknown as SettingsApi;

  renderSecurity(store, api);
  if (options.waitForDraft !== false) {
    // 草稿就绪 = 开关出现。R13 后页脚常驻（它同时承担改密提交），不能再拿页脚当草稿就绪信号。
    await screen.findByRole('switch', { name: copy.settings.security.abOptOutLabel });
  }
  return { store, getPreferences, updatePreferences, changePassword };
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

  it('logs out all devices only after a danger ConfirmDialog (A38)', async () => {
    const { store, api: authApi } = await createAuthedStore({
      listSessions: vi.fn(async () => [CURRENT_SESSION]),
    });
    const revokeAllSessions = vi.spyOn(store, 'revokeAllSessions');
    const settingsApi = { changePassword: vi.fn(async () => {}) } as unknown as SettingsApi;
    const user = userEvent.setup();

    renderSecurity(store, settingsApi);
    await screen.findByText(CURRENT_SESSION.device);

    await user.click(screen.getByRole('button', { name: copy.settings.security.logoutAll }));
    // 未确认前不执行
    expect(revokeAllSessions).not.toHaveBeenCalled();
    const dialog = await screen.findByRole('dialog', {
      name: copy.settings.security.logoutAllConfirmTitle,
    });
    expect(within(dialog).getByText(copy.settings.security.logoutAllConfirmDescription)).toBeInTheDocument();

    await user.click(within(dialog).getByRole('button', { name: copy.settings.security.logoutAll }));

    await waitFor(() => expect(revokeAllSessions).toHaveBeenCalledOnce());
    expect(authApi.revokeAllSessions).toHaveBeenCalledOnce();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('cancels the logout-all ConfirmDialog with Esc without revoking anything (A38 Esc=取消)', async () => {
    const { store } = await createAuthedStore({
      listSessions: vi.fn(async () => [CURRENT_SESSION]),
    });
    const revokeAllSessions = vi.spyOn(store, 'revokeAllSessions');
    const settingsApi = { changePassword: vi.fn(async () => {}) } as unknown as SettingsApi;
    const user = userEvent.setup();

    renderSecurity(store, settingsApi);
    await screen.findByText(CURRENT_SESSION.device);
    await user.click(screen.getByRole('button', { name: copy.settings.security.logoutAll }));
    expect(await screen.findByRole('dialog', { name: copy.settings.security.logoutAllConfirmTitle })).toBeInTheDocument();

    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(revokeAllSessions).not.toHaveBeenCalled();
    expect(store.getState().status).toBe('authenticated');
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
    const dialog = await screen.findByRole('dialog', { name: copy.settings.security.logoutAllConfirmTitle });
    await user.click(within(dialog).getByRole('button', { name: copy.settings.security.logoutAll }));

    expect(await screen.findByRole('alert')).toHaveTextContent(copy.settings.security.sessionActionError);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(store.getState().status).toBe('authenticated');
    expect(screen.getByText(CURRENT_SESSION.device)).toBeInTheDocument();
    expect(screen.getByText(OTHER_SESSION.device)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: copy.settings.security.logoutAll })).toBeEnabled();
  });

  it('shows the all-devices sign-out note as the 活跃会话 row description, not in the footer (A37)', async () => {
    const { store } = await createAuthedStore();
    const settingsApi = { changePassword: vi.fn(async () => {}) } as unknown as SettingsApi;
    renderSecurity(store, settingsApi);

    // R14 精修：设计图的行序列里没有独立的 A37 行，说明改作「活跃会话」行的左列说明；
    // 页脚只留共享原语的「取消 / 保存」两个键（statusSlot 不传）
    const note = await screen.findByText(copy.settings.security.passwordSessionNote);
    const sessionsRow = screen.getAllByTestId('form-row')[3];
    expect(sessionsRow.querySelector('[data-testid="form-row-label"]')).toContainElement(note);
    const footer = screen.getByTestId('form-footer');
    expect(footer).not.toContainElement(note);
    expect(Array.from(footer.children)).toHaveLength(2);
  });

  it('三个密码字段默认掩码，点眼睛切换明文且不改字段取值，再次点击恢复掩码（A37）', async () => {
    const { store } = await createAuthedStore();
    const settingsApi = { changePassword: vi.fn(async () => {}) } as unknown as SettingsApi;
    const user = userEvent.setup();

    renderSecurity(store, settingsApi);

    const fields = [
      {
        label: copy.settings.security.oldPasswordLabel,
        show: copy.settings.security.showOldPassword,
        hide: copy.settings.security.hideOldPassword,
      },
      {
        label: copy.settings.security.newPasswordLabel,
        show: copy.settings.security.showNewPassword,
        hide: copy.settings.security.hideNewPassword,
      },
      {
        label: copy.settings.security.confirmPasswordLabel,
        show: copy.settings.security.showConfirmPassword,
        hide: copy.settings.security.hideConfirmPassword,
      },
    ];

    // 可访问名按字段限定：同页三个眼睛按钮各有唯一名字，读屏用户才分得清是哪个字段
    // （初始都是掩码态，故此刻按钮名是各自的「显示…」）
    for (const field of fields) {
      expect(screen.getAllByRole('button', { name: field.show })).toHaveLength(1);
    }

    for (const field of fields) {
      const input = screen.getByLabelText(field.label);
      // 眼睛按钮与该字段同一行
      const row = input.closest('[data-testid="form-row"]') as HTMLElement | null;
      expect(row).not.toBeNull();
      const scope = within(row as HTMLElement);

      await user.type(input, 'Abcd1234');
      expect(input).toHaveAttribute('type', 'password');

      const showToggle = scope.getByRole('button', { name: field.show });
      expect(showToggle).toHaveAttribute('aria-pressed', 'false');
      // 眼睛图标是可聚焦按钮（辅助技术可达）
      showToggle.focus();
      expect(showToggle).toHaveFocus();
      await user.click(showToggle);

      expect(input).toHaveAttribute('type', 'text');
      // 切换显示形态不得改变字段取值
      expect(input).toHaveValue('Abcd1234');
      // 明文态的「隐藏…」名同样全页唯一（此刻只有本字段是明文）
      expect(screen.getAllByRole('button', { name: field.hide })).toHaveLength(1);
      expect(scope.getByRole('button', { name: field.hide })).toHaveAttribute('aria-pressed', 'true');

      // 再次点击恢复掩码，取值仍不变
      await user.click(scope.getByRole('button', { name: field.hide }));
      expect(input).toHaveAttribute('type', 'password');
      expect(input).toHaveValue('Abcd1234');
    }
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

describe('SecurityModule 表单结构（设置基座：整页一张卡片、连续两栏行）', () => {
  it('页面只有一张设置卡片，卡内没有小节标题，行序为「三个密码行 → 活跃会话行 → 会话行 → 隐私行」', async () => {
    await renderModule({
      auth: { listSessions: vi.fn(async () => [CURRENT_SESSION, OTHER_SESSION]) },
    });

    const cards = screen.getAllByTestId('settings-card');
    expect(cards).toHaveLength(1);
    expect(cards[0]).toHaveAccessibleName(copy.settings.security.cardTitle);
    // R14 + 3.8：设计图是「卡片标题 + 副标题 + 连续的两栏表单行」，卡内只有卡片标题一个标题元素，
    // 没有 h2 小节标题（分区由表单行的标签表达）
    expect(screen.queryAllByRole('heading')).toHaveLength(1);
    expect(cards[0].querySelectorAll('h1, h2, h3, h4, h5, h6')).toHaveLength(1);

    const rows = screen.getAllByTestId('form-row');
    expect(rows).toHaveLength(7);
    const labels = screen.getAllByTestId('form-row-label').map((cell) => cell.textContent ?? '');
    expect(labels[0]).toContain(copy.settings.security.oldPasswordLabel);
    expect(labels[1]).toContain(copy.settings.security.newPasswordLabel);
    expect(labels[2]).toContain(copy.settings.security.confirmPasswordLabel);
    expect(labels[3]).toContain(copy.settings.security.sessionsTitle);
    expect(labels[4]).toContain(CURRENT_SESSION.device);
    expect(labels[5]).toContain(OTHER_SESSION.device);
    expect(labels[6]).toContain(copy.settings.security.abOptOutLabel);
  });

  it('卡片在第一个表单行之前渲染模块标题与灰色副标题（措辞逐字取自设计图）', async () => {
    await renderModule();

    const card = screen.getByTestId('settings-card');
    const heading = screen.getByRole('heading', { level: 2 });
    expect(card).toContainElement(heading);
    expect(heading).toHaveTextContent('安全设置');
    expect(heading.tagName).toBe('H2');
    expect(heading.className.split(/\s+/)).toContain('text-body-lg');

    const subtitle = screen.getByText('管理登录密码与设备会话');
    expect(subtitle.tagName).toBe('P');
    expect(subtitle.className.split(/\s+/)).toContain('text-caption');
    expect(subtitle.className.split(/\s+/)).toContain('text-slate-strong');

    expect(heading.compareDocumentPosition(subtitle) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(
      subtitle.compareDocumentPosition(screen.getAllByTestId('form-row')[0]) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('区域只在卡片上命名一次，且区域名即可见标题（无嵌套同名 landmark）', async () => {
    await renderModule();

    const regions = screen.getAllByRole('region');
    expect(regions).toHaveLength(1);
    expect(regions[0]).toBe(screen.getByTestId('settings-card'));
    // 区域名必须等于卡内可见标题（不再取可能与标题漂移的 sectionLabel「安全」）
    const heading = screen.getByRole('heading', { level: 2 });
    expect(regions[0]).toHaveAccessibleName(heading.textContent ?? '');
    expect(regions[0]).toHaveAccessibleName(copy.settings.security.cardTitle);
    expect(regions[0]).not.toHaveAttribute('aria-label');
    // 标题元素自身不挂 aria-label（同一元素上标题 + aria-label 会被重复朗读）
    expect(heading).not.toHaveAttribute('aria-label');
  });

  it('只有一个承载末行分隔线关闭规则的行容器，且它的最后一个子节点是末行（隐私行）', async () => {
    await renderModule({
      auth: { listSessions: vi.fn(async () => [CURRENT_SESSION, OTHER_SESSION]) },
    });

    const card = screen.getByTestId('settings-card');
    const rows = screen.getAllByTestId('form-row');
    // 末行（隐私行）的直接父节点就是那个承载规则的容器（密码三行套在 form 里，父节点是 form）
    const container = rows[rows.length - 1].parentElement as HTMLElement;
    expect(container.className.split(/\s+/)).toContain('[&>*:last-child]:border-b-0');
    expect(card).toContainElement(container);
    expect(container.lastElementChild).toBe(rows[rows.length - 1]);
    // 容器是卡片「标题块之后」的第一个子节点（卡片首个子节点是标题块）；
    // 条件渲染的提示都在容器之外，不会顶掉末行的分隔线关闭规则
    expect(card.children[0]).toContainElement(screen.getByRole('heading', { level: 2 }));
    expect(card.children[1]).toBe(container);
    for (const row of rows) {
      // 每一行都在这个容器内（密码三行隔着一层 form，其余行是直接子节点）
      expect(container).toContainElement(row);
      expect(row.closest('[data-testid="settings-card"]')).toBe(card);
    }
    // 该规则不得加在卡片上：卡片的最后一个子节点是 FormFooter，加在卡片上命中的会是页脚
    expect(card.className.split(/\s+/)).not.toContain('[&>*:last-child]:border-b-0');
  });

  it('FormFooter 是唯一卡片的直接子节点且为最后一个子节点', async () => {
    await renderModule();

    const card = screen.getByTestId('settings-card');
    const footer = screen.getByTestId('form-footer');
    expect(footer.parentElement).toBe(card);
    expect(card.lastElementChild).toBe(footer);
    // 全页只有一份页脚、一个「保存」：退出类动作是即时动作，不产生第二个提交入口
    expect(screen.getAllByTestId('form-footer')).toHaveLength(1);
    expect(screen.getAllByRole('button', { name: copy.controls.save })).toHaveLength(1);
  });

  it('活跃会话行：左列标签「活跃会话」，右列是「退出全部设备」危险链接（即时动作）', async () => {
    await renderModule({
      auth: { listSessions: vi.fn(async () => [CURRENT_SESSION, OTHER_SESSION]) },
    });

    const row = screen.getAllByTestId('form-row')[3];
    const labelCell = row.querySelector('[data-testid="form-row-label"]') as HTMLElement;
    expect(labelCell.textContent).toContain(copy.settings.security.sessionsTitle);

    const signOutAll = within(row).getByRole('button', { name: copy.settings.security.logoutAll });
    // 控件在右列，不在左列标签位
    expect(labelCell).not.toContainElement(signOutAll);
    expect(signOutAll.className.split(/\s+/)).toContain('text-danger');
  });

  it('隐私行：左列标签 + 说明，右列是开关', async () => {
    await renderModule();

    const rows = screen.getAllByTestId('form-row');
    const row = rows[rows.length - 1];
    const labelCell = row.querySelector('[data-testid="form-row-label"]') as HTMLElement;
    expect(labelCell.textContent).toContain(copy.settings.security.abOptOutLabel);
    expect(labelCell.textContent).toContain(copy.settings.security.abOptOutDescription);

    const toggle = within(row).getByRole('switch', {
      name: copy.settings.security.abOptOutLabel,
    });
    expect(labelCell).not.toContainElement(toggle);
  });

  it('密码三行套在 form 内（Enter 隐式提交的载体），表单是行容器的第一个子节点', async () => {
    await renderModule();

    const rows = screen.getAllByTestId('form-row');
    const container = rows[rows.length - 1].parentElement as HTMLElement;
    const form = container.querySelector('form') as HTMLFormElement;
    expect(form).not.toBeNull();
    expect(container.children[0]).toBe(form);
    // 三个密码行在 form 内，其余行在 form 外
    for (const row of rows.slice(0, 3)) {
      expect(form).toContainElement(row);
    }
    for (const row of rows.slice(3)) {
      expect(form).not.toContainElement(row);
    }
  });

  it('三行密码各有左列说明（措辞取自设计图），控件列不再重复同一段文字', async () => {
    await renderModule();

    const rows = screen.getAllByTestId('form-row').slice(0, 3);
    const fields = [
      {
        label: copy.settings.security.oldPasswordLabel,
        hint: copy.settings.security.oldPasswordHint,
      },
      {
        label: copy.settings.security.newPasswordLabel,
        hint: copy.settings.security.passwordRule,
      },
      {
        label: copy.settings.security.confirmPasswordLabel,
        hint: copy.settings.security.confirmPasswordHint,
      },
    ];

    fields.forEach((field, index) => {
      const labelCell = rows[index].querySelector('[data-testid="form-row-label"]') as HTMLElement;
      expect(labelCell.textContent).toContain(field.label);
      expect(labelCell.textContent).toContain(field.hint);
      // 说明只在左列；右列不再出现同一段文字（旧结构把规则放在框下方）
      const controlCell = labelCell.nextElementSibling as HTMLElement;
      expect(controlCell.textContent).not.toContain(field.hint);
    });

    // 三行结构一致（每行左列都是「标签 + 一条说明」），行高因此趋于一致（实测数字见报告）
    for (const row of rows) {
      expect(row.querySelectorAll('[data-testid="form-row-label"] p')).toHaveLength(1);
    }
  });

  it('密码行标签关联对应输入框，id 与改造前一致', async () => {
    await renderModule();

    const cells = screen.getAllByTestId('form-row-label').slice(0, 3);
    const labels = cells.map((cell) => cell.textContent ?? '');
    expect(labels[0]).toContain(copy.settings.security.oldPasswordLabel);
    expect(labels[1]).toContain(copy.settings.security.newPasswordLabel);
    expect(labels[2]).toContain(copy.settings.security.confirmPasswordLabel);
    expect(cells.map((cell) => cell.querySelector('label')?.getAttribute('for'))).toEqual([
      'settings-old-password',
      'settings-new-password',
      'settings-confirm-password',
    ]);
  });

  it('会话行每个会话一行：设备名在标签位、最近活跃时间在说明位、行尾是退出动作', async () => {
    await renderModule({
      auth: { listSessions: vi.fn(async () => [CURRENT_SESSION, OTHER_SESSION]) },
    });

    const sessionRows = screen.getAllByTestId('form-row').slice(4, 6);
    expect(sessionRows[0].querySelector('[data-testid="form-row-label"]')?.textContent).toContain(
      CURRENT_SESSION.device,
    );
    expect(sessionRows[1].querySelector('[data-testid="form-row-label"]')?.textContent).toContain(
      OTHER_SESSION.device,
    );
    // 最近活跃时间落在行的说明位（文案模板取自 copy，不硬编码 "最近活跃"）
    expect(sessionRows[0].textContent).toContain(copy.settings.security.lastActiveAt(''));
    // 行尾动作：当前设备行为「退出登录」，其余为「退出此设备」
    expect(
      within(sessionRows[0]).getByRole('button', { name: copy.settings.security.logoutCurrent }),
    ).toBeInTheDocument();
    expect(
      within(sessionRows[1]).getByRole('button', { name: copy.settings.security.logoutOther }),
    ).toBeInTheDocument();
  });
});

describe('SecurityModule 即时动作边界（退出类动作不进草稿）', () => {
  it('退出全部设备用危险色，二次确认后立即执行，不经过「保存」', async () => {
    const { store } = await createAuthedStore({
      listSessions: vi.fn(async () => [CURRENT_SESSION]),
    });
    const revokeAllSessions = vi.spyOn(store, 'revokeAllSessions');
    const updatePreferences = vi.fn(async (next: UserPreferences) => echoPreferences(next));
    const settingsApi = {
      changePassword: vi.fn(async () => {}),
      updatePreferences,
    } as unknown as SettingsApi;
    const user = userEvent.setup();

    renderSecurity(store, settingsApi);
    const signOutAll = await screen.findByRole('button', { name: copy.settings.security.logoutAll });
    // 危险色走抽屉作用域 token（text-danger），不写死 hex
    expect(signOutAll.className.split(/\s+/)).toContain('text-danger');
    expect(signOutAll.className).not.toMatch(/#[0-9a-f]{3,8}/i);

    // A38 危险确认框：确认后立即执行，不等「保存」
    await user.click(signOutAll);
    const dialog = await screen.findByRole('dialog', {
      name: copy.settings.security.logoutAllConfirmTitle,
    });
    await user.click(within(dialog).getByRole('button', { name: copy.settings.security.logoutAll }));

    await waitFor(() => expect(revokeAllSessions).toHaveBeenCalledTimes(1));
    expect(updatePreferences).not.toHaveBeenCalled();
  });

  it('退出此设备点击即执行，不使隐私草稿进入待保存态，也不因「取消」撤销', async () => {
    const { store } = await createAuthedStore({
      listSessions: vi.fn(async () => [CURRENT_SESSION, OTHER_SESSION]),
    });
    const revokeSession = vi.spyOn(store, 'revokeSession');
    const updatePreferences = vi.fn(async (next: UserPreferences) => echoPreferences(next));
    const settingsApi = {
      changePassword: vi.fn(async () => {}),
      updatePreferences,
    } as unknown as SettingsApi;
    const user = userEvent.setup();

    renderSecurity(store, settingsApi);
    await screen.findByText(OTHER_SESSION.device);

    await user.click(screen.getByRole('button', { name: copy.settings.security.logoutOther }));
    await waitFor(() =>
      expect(revokeSession).toHaveBeenCalledWith(OTHER_SESSION.id, { current: false }),
    );

    // 即时动作不使表单 dirty：「保存」不产生偏好写入，「取消」也不撤销已生效的退出
    await user.click(cancelButton());
    await user.click(saveButton());
    expect(updatePreferences).not.toHaveBeenCalled();
    expect(screen.queryByText(OTHER_SESSION.device)).not.toBeInTheDocument();
  });

  it('退出全部设备 / 退出此设备 / 退出登录三个动作都以危险色呈现，可访问名即动作本身', async () => {
    await renderModule({
      auth: { listSessions: vi.fn(async () => [CURRENT_SESSION, OTHER_SESSION]) },
    });

    for (const name of [
      copy.settings.security.logoutAll,
      copy.settings.security.logoutOther,
      copy.settings.security.logoutCurrent,
    ]) {
      const action = screen.getByRole('button', { name });
      expect(action.className.split(/\s+/)).toContain('text-danger');
      expect(action).toBeEnabled();
    }
  });
});

describe('SecurityModule 隐私开关的草稿-保存语义（共用基座 §5.4 / §5.5）', () => {
  it('隐私行呈现标签 + 说明 + 开关，开关反映已加载的 ab_opt_out 偏好', async () => {
    await renderModule({
      settings: { getPreferences: vi.fn(async () => ({ ...PREFERENCES, ab_opt_out: true })) },
    });

    expect(screen.getByText(copy.settings.security.abOptOutLabel)).toBeInTheDocument();
    expect(screen.getByText(copy.settings.security.abOptOutDescription)).toBeInTheDocument();
    expect(privacySwitch()).toHaveAttribute('data-state', 'checked');
  });

  it('隐私开关走草稿：切换只改本地草稿，点「保存」才写偏好', async () => {
    const { updatePreferences } = await renderModule();
    const user = userEvent.setup();

    expect(privacySwitch()).toHaveAttribute('data-state', 'unchecked');
    await user.click(privacySwitch());

    // 草稿即时反映选择，但偏好未被写入
    expect(privacySwitch()).toHaveAttribute('data-state', 'checked');
    expect(updatePreferences).not.toHaveBeenCalled();

    await user.click(saveButton());

    await waitFor(() =>
      expect(updatePreferences).toHaveBeenCalledWith(expect.objectContaining({ ab_opt_out: true })),
    );
    expect(privacySwitch()).toHaveAttribute('data-state', 'checked');
  });

  it('保存提交完整快照，含未在本模块渲染的字段', async () => {
    const { updatePreferences } = await renderModule({
      settings: {
        getPreferences: vi.fn(async () => ({ theme: 'dark', chat_font_size: 'large', ab_opt_out: false })),
      },
    });
    const user = userEvent.setup();

    await user.click(privacySwitch());
    await user.click(saveButton());

    await waitFor(() =>
      expect(updatePreferences).toHaveBeenCalledWith({
        theme: 'dark',
        chat_font_size: 'large',
        ab_opt_out: true,
      }),
    );
  });

  it('「取消」丢弃草稿：开关回到已提交取值，不写偏好', async () => {
    const { updatePreferences } = await renderModule();
    const user = userEvent.setup();

    await user.click(privacySwitch());
    expect(privacySwitch()).toHaveAttribute('data-state', 'checked');

    await user.click(cancelButton());

    expect(privacySwitch()).toHaveAttribute('data-state', 'unchecked');
    expect(updatePreferences).not.toHaveBeenCalled();
  });

  it('保存进行中隐私开关不可交互且禁用态可见（R11 保存期禁用契约）', async () => {
    // 回归守卫：真实 API 的保存响应每次都是新对象（api/client.ts `response.json()`），
    // useDraftForm 见 submitted 变化即重置草稿。若保存窗口内开关仍可改，那笔在途编辑会在
    // 响应落地时被静默丢弃。
    const save = deferred<UserPreferences>();
    const updatePreferences = vi.fn<SettingsApi['updatePreferences']>(() => save.promise);
    await renderModule({ settings: { updatePreferences } });
    const user = userEvent.setup();

    await user.click(privacySwitch());
    await user.click(saveButton());
    await waitFor(() => expect(saveButton()).toBeDisabled());

    expect(cancelButton()).toBeDisabled();
    expect(privacySwitch()).toBeDisabled();
    // 禁用态可见：纳入草稿的控件的禁用外壳带 R11 要求的两个类
    const shell = privacySwitch().closest('fieldset');
    expect(shell).not.toBeNull();
    expect(shell?.className.split(/\s+/)).toContain('disabled:opacity-60');
    expect(shell?.className.split(/\s+/)).toContain('disabled:cursor-not-allowed');

    // 在途点击被吞掉：草稿仍是提交前那次选择
    await user.click(privacySwitch());
    expect(privacySwitch()).toHaveAttribute('data-state', 'checked');

    // 保存落地（新对象身份 → 草稿重置为服务端快照）后仍是这次选择，且再点保存是 no-op
    await act(async () => {
      save.resolve(echoPreferences({ ...PREFERENCES, ab_opt_out: true }));
      await save.promise;
    });
    expect(privacySwitch()).toHaveAttribute('data-state', 'checked');
    await user.click(saveButton());
    expect(updatePreferences).toHaveBeenCalledTimes(1);
  });

  it('保存失败：开关回滚到已提交取值并就地提示', async () => {
    const updatePreferences = vi.fn(async (_next: UserPreferences) => {
      throw new Error('offline');
    });
    await renderModule({ settings: { updatePreferences } });
    const user = userEvent.setup();

    await user.click(privacySwitch());
    await user.click(saveButton());

    expect(await screen.findByRole('alert')).toHaveTextContent(
      copy.settings.security.preferencesSaveError,
    );
    expect(privacySwitch()).toHaveAttribute('data-state', 'unchecked');
  });

  it('偏好加载完成前只渲染加载行、不渲染隐私行；此间点「保存」是 no-op', async () => {
    const pending = deferred<UserPreferences>();
    const getPreferences = vi.fn<SettingsApi['getPreferences']>(() => pending.promise);

    const { updatePreferences } = await renderModule({ settings: { getPreferences }, waitForDraft: false });

    expect(screen.getByText(copy.settings.security.preferencesLoading)).toBeInTheDocument();
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
    // 隐私行是草稿就绪后才渲染的最后一行；加载期行序到会话区为止
    expect(screen.getAllByTestId('form-row')).toHaveLength(4);

    // R13：页脚常驻（同一份页脚也承担改密提交），但草稿未就绪时提交不产生偏好写入
    await userEvent.setup().click(saveButton());
    expect(updatePreferences).not.toHaveBeenCalled();
    expect(await screen.findByTestId('form-footer')).toBeInTheDocument();

    await act(async () => {
      pending.resolve(echoPreferences(PREFERENCES));
      await pending.promise;
    });
    expect(privacySwitch()).toHaveAttribute('data-state', 'unchecked');
  });

  it('偏好加载失败：卡内就地错误行 + 重试文字链，不渲染开关', async () => {
    const getPreferences = vi.fn<SettingsApi['getPreferences']>(async () => {
      throw new Error('offline');
    });

    await renderModule({ settings: { getPreferences }, waitForDraft: false });

    expect(
      await screen.findByText(copy.settings.security.preferencesLoadError),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: copy.states.retry })).toBeInTheDocument();
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
  });
});

describe('SecurityModule 页脚提交语义（R13：整页一张卡片、一个页脚）', () => {
  const typePasswords = async (
    user: ReturnType<typeof userEvent.setup>,
    oldPassword = 'password123',
    newPassword = 'newpassword1',
  ): Promise<void> => {
    await user.type(screen.getByLabelText(copy.settings.security.oldPasswordLabel), oldPassword);
    await user.type(screen.getByLabelText(copy.settings.security.newPasswordLabel), newPassword);
    await user.type(screen.getByLabelText(copy.settings.security.confirmPasswordLabel), newPassword);
  };

  it('密码字段非空时「保存」提交改密，未改动的隐私草稿不写偏好', async () => {
    const { changePassword, updatePreferences } = await renderModule();
    const user = userEvent.setup();

    await typePasswords(user);
    // 只是输入不改请求（显式提交语义不变）
    expect(changePassword).not.toHaveBeenCalled();

    await user.click(saveButton());

    await waitFor(() =>
      expect(changePassword).toHaveBeenCalledWith({
        old_password: 'password123',
        new_password: 'newpassword1',
      }),
    );
    expect(updatePreferences).not.toHaveBeenCalled();
  });

  it('两者都有改动时「保存」一并提交改密与隐私偏好', async () => {
    const { changePassword, updatePreferences } = await renderModule();
    const user = userEvent.setup();

    await typePasswords(user);
    await user.click(privacySwitch());
    await user.click(saveButton());

    await waitFor(() =>
      expect(changePassword).toHaveBeenCalledWith({
        old_password: 'password123',
        new_password: 'newpassword1',
      }),
    );
    await waitFor(() =>
      expect(updatePreferences).toHaveBeenCalledWith(expect.objectContaining({ ab_opt_out: true })),
    );
  });

  it('两者都没有改动时「保存」是 no-op', async () => {
    const { changePassword, updatePreferences } = await renderModule();
    const user = userEvent.setup();

    await user.click(saveButton());

    expect(changePassword).not.toHaveBeenCalled();
    expect(updatePreferences).not.toHaveBeenCalled();
  });

  it('密码校验失败：就地报错且不发请求，隐私草稿仍照常提交', async () => {
    const { changePassword, updatePreferences } = await renderModule();
    const user = userEvent.setup();

    await typePasswords(user, 'password123', 'letters');
    await user.click(privacySwitch());
    await user.click(saveButton());

    expect(await screen.findByRole('alert')).toHaveTextContent(
      copy.settings.security.invalidPasswordRule,
    );
    expect(changePassword).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(updatePreferences).toHaveBeenCalledWith(expect.objectContaining({ ab_opt_out: true })),
    );
  });

  it('「取消」清空三个密码字段（含就地错误行）并复位隐私开关，不提交任何请求', async () => {
    const { changePassword, updatePreferences } = await renderModule();
    const user = userEvent.setup();

    // 先制造一条就地错误行（本地校验失败，不发请求）
    await typePasswords(user, 'password123', 'letters');
    await user.click(saveButton());
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(changePassword).not.toHaveBeenCalled();

    // 再留下一个未保存的隐私草稿
    await user.click(privacySwitch());
    expect(privacySwitch()).toHaveAttribute('data-state', 'checked');

    await user.click(cancelButton());

    for (const label of [
      copy.settings.security.oldPasswordLabel,
      copy.settings.security.newPasswordLabel,
      copy.settings.security.confirmPasswordLabel,
    ]) {
      expect(screen.getByLabelText(label)).toHaveValue('');
    }
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(privacySwitch()).toHaveAttribute('data-state', 'unchecked');
    expect(changePassword).not.toHaveBeenCalled();
    expect(updatePreferences).not.toHaveBeenCalled();
  });

  it('密码框内按 Enter 仍提交改密（可见提交键移到页脚后不得失去隐式提交）', async () => {
    const { changePassword } = await renderModule();
    const user = userEvent.setup();

    await typePasswords(user);
    await user.type(
      screen.getByLabelText(copy.settings.security.confirmPasswordLabel),
      '{Enter}',
    );

    await waitFor(() =>
      expect(changePassword).toHaveBeenCalledWith({
        old_password: 'password123',
        new_password: 'newpassword1',
      }),
    );
  });
});
