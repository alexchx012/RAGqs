import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type { AuthApi } from '../auth/api';
import { AuthProvider } from '../auth/AuthProvider';
import { createMemoryAuthHub } from '../auth/channel';
import { AuthSessionStore } from '../auth/session';
import type { User } from '../auth/types';
import { copy } from '../copy';
import type { NotificationsStore } from '../notifications/store';
import type { ThemeController } from '../theme/theme';
import type { SettingsApi } from './api';
import { ProfileModule } from './ProfileModule';
import { SettingsProvider } from './SettingsProvider';

function testUser(overrides: Partial<User> = {}): User {
  return {
    id: 'u_1',
    username: 'zhangsan',
    display_name: '张三',
    real_name: '张三',
    department: { id: 'd_finance', name: '财务部' },
    role: 'user',
    avatar_url: '/avatars/before.png',
    ...overrides,
  };
}

async function createAuthedStore(user: User): Promise<AuthSessionStore> {
  const api: AuthApi = {
    login: vi.fn(async () => ({ token: 'tok_login', user })),
    logout: vi.fn(async () => {}),
    refresh: vi.fn(async () => ({ token: 'tok_refresh' })),
    me: vi.fn(async () => user),
    listSessions: vi.fn(async () => []),
    revokeSession: vi.fn(async () => {}),
    revokeAllSessions: vi.fn(async () => {}),
  };
  const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
  await store.login('zhangsan', 'password123');
  return store;
}

function renderProfile(store: AuthSessionStore, api: SettingsApi) {
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
        <ProfileModule />
      </SettingsProvider>
    </AuthProvider>,
  );
}

describe('ProfileModule', () => {
  it('only submits the edited display name and synchronizes the returned presentation value', async () => {
    const user = userEvent.setup();
    const currentUser = testUser();
    const updateProfile = vi.fn(async (input: { display_name: string }) => ({
      ...currentUser,
      display_name: input.display_name,
    }));
    const api = {
      updateProfile,
      uploadAvatar: vi.fn(async () => ({ avatar_url: '/avatars/unused.png' })),
    } as unknown as SettingsApi;
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    const input = screen.getByLabelText(copy.settings.profile.displayNameLabel);
    await user.clear(input);
    await user.type(input, '新名字');
    await user.click(screen.getByRole('button', { name: copy.settings.profile.save }));

    await waitFor(() => {
      expect(updateProfile).toHaveBeenCalledWith({ display_name: '新名字' });
    });
    expect(store.getState().user?.display_name).toBe('新名字');
    expect(input).toHaveValue('新名字');
  });

  it('uploads one avatar file and immediately updates the rendered avatar source from the response', async () => {
    const user = userEvent.setup();
    const uploadAvatar = vi.fn(async () => ({ avatar_url: '/avatars/after.png' }));
    const api = {
      updateProfile: vi.fn(async (input: { display_name: string }) => ({
        ...testUser(),
        display_name: input.display_name,
      })),
      uploadAvatar,
    } as unknown as SettingsApi;
    const store = await createAuthedStore(testUser());

    renderProfile(store, api);
    const avatar = screen.getByRole('img', { name: copy.settings.profile.avatarAlt });
    expect(avatar).toHaveAttribute('src', '/avatars/before.png');

    const file = new File(['avatar'], 'next-avatar.png', { type: 'image/png' });
    await user.upload(screen.getByLabelText(copy.settings.profile.avatarInputLabel), file);

    await waitFor(() => expect(uploadAvatar).toHaveBeenCalledWith(file));
    expect(avatar).toHaveAttribute('src', '/avatars/after.png');
    expect(store.getState().user?.avatar_url).toBe('/avatars/after.png');
  });

  it('keeps real name, department, and role read-only with administrator-maintained guidance', async () => {
    const store = await createAuthedStore(testUser());
    const api = {
      updateProfile: vi.fn(),
      uploadAvatar: vi.fn(),
    } as unknown as SettingsApi;

    renderProfile(store, api);

    await screen.findByText(copy.settings.profile.realNameLabel);
    expect(screen.getByText(copy.settings.profile.realNameLabel)).toBeInTheDocument();
    expect(screen.getByText('张三')).toBeInTheDocument();
    expect(screen.getByText(copy.settings.profile.departmentLabel)).toBeInTheDocument();
    expect(screen.getByText('财务部')).toBeInTheDocument();
    expect(screen.getByText(copy.settings.profile.roleLabel)).toBeInTheDocument();
    expect(screen.getByText(copy.settings.profile.roleUser)).toBeInTheDocument();
    expect(screen.getAllByText(copy.settings.profile.adminManaged)).toHaveLength(3);
  });
});

describe('ProfileModule 保存交互（共用基座 §5.3）', () => {
  it('无未保存变更时不渲染「保存」「取消」', async () => {
    const store = await createAuthedStore(testUser());
    const api = {
      updateProfile: vi.fn(),
      uploadAvatar: vi.fn(),
    } as unknown as SettingsApi;

    renderProfile(store, api);
    await screen.findByText(copy.settings.profile.realNameLabel);

    expect(screen.queryByRole('button', { name: copy.settings.profile.save })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: copy.controls.cancel })).not.toBeInTheDocument();
  });

  it('编辑后出现「保存」「取消」；「取消」回退未保存变更', async () => {
    const user = userEvent.setup();
    const store = await createAuthedStore(testUser());
    const api = {
      updateProfile: vi.fn(),
      uploadAvatar: vi.fn(),
    } as unknown as SettingsApi;

    renderProfile(store, api);
    const input = screen.getByLabelText(copy.settings.profile.displayNameLabel);
    await user.clear(input);
    await user.type(input, '新名字');

    const saveButton = await screen.findByRole('button', { name: copy.settings.profile.save });
    const cancelButton = screen.getByRole('button', { name: copy.controls.cancel });
    expect(saveButton).toBeInTheDocument();
    expect(cancelButton).toBeInTheDocument();

    await user.click(cancelButton);
    expect(input).toHaveValue('张三');
    expect(screen.queryByRole('button', { name: copy.settings.profile.save })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: copy.controls.cancel })).not.toBeInTheDocument();
    expect(api.updateProfile).not.toHaveBeenCalled();
  });

  it('保存成功：按钮旁淡入「已保存」小字，约 2s 后淡出消失', async () => {
    const user = userEvent.setup();
    const currentUser = testUser();
    const updateProfile = vi.fn(async (input: { display_name: string }) => ({
      ...currentUser,
      display_name: input.display_name,
    }));
    const api = {
      updateProfile,
      uploadAvatar: vi.fn(),
    } as unknown as SettingsApi;
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    const input = screen.getByLabelText(copy.settings.profile.displayNameLabel);
    await user.clear(input);
    await user.type(input, '新名字');
    await user.click(screen.getByRole('button', { name: copy.settings.profile.save }));

    // 成功反馈出现在按钮旁（操作行内），随后约 2s 淡出卸载
    const feedback = await screen.findByText(copy.settings.profile.saved);
    expect(feedback).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(copy.settings.profile.saved)).not.toBeInTheDocument(), {
      timeout: 3000,
    });
  });

  it('保存失败：就地错误行且按钮恢复可点', async () => {
    const user = userEvent.setup();
    const updateProfile = vi.fn(async (_input: { display_name: string }) => {
      throw new Error('offline');
    });
    const api = {
      updateProfile,
      uploadAvatar: vi.fn(),
    } as unknown as SettingsApi;
    const store = await createAuthedStore(testUser());

    renderProfile(store, api);
    const input = screen.getByLabelText(copy.settings.profile.displayNameLabel);
    await user.clear(input);
    await user.type(input, '新名字');
    await user.click(screen.getByRole('button', { name: copy.settings.profile.save }));

    expect(await screen.findByRole('alert')).toHaveTextContent(copy.settings.profile.saveError);
    const saveButton = screen.getByRole('button', { name: copy.settings.profile.save });
    await waitFor(() => expect(saveButton).toBeEnabled());
    expect(screen.queryByText(copy.settings.profile.saved)).not.toBeInTheDocument();
  });
});
