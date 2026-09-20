import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router';
import type { AuthApi } from '../auth/api';
import { AuthProvider } from '../auth/AuthProvider';
import { createMemoryAuthHub } from '../auth/channel';
import { AuthSessionStore } from '../auth/session';
import type { User } from '../auth/types';
import { copy } from '../copy';
import { EscStackProvider } from '../lib/esc-stack-provider';
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
    // A39：未保存更改的关闭拦截经 URL 关闭抽屉（useNavigate），测试以 MemoryRouter 承载
    <MemoryRouter initialEntries={['/settings']}>
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
            <LocationProbe />
            <button type="button" aria-label={copy.shell.drawer.closeAria}>
              drawer-close
            </button>
            <ProfileModule />
          </SettingsProvider>
        </AuthProvider>
      </EscStackProvider>
    </MemoryRouter>,
  );
}

/** location 探针（A39：确认放弃后断言抽屉关闭导航到根路径）。 */
function LocationProbe() {
  const location = useLocation();
  return <output data-testid="profile-location">{location.pathname}</output>;
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

/*
 * 保存回声必须还原真实 API 的对象身份行为：api/client.ts 的 `response.json()` 每次返回
 * **新对象**。若 mock 直接回声传入的引用，草稿层就观察不到 submitted 变化，会掩盖
 * 「保存落地即重置草稿」这类缺陷（Task 8 教训）。故统一经本函数：取值相同、身份必新。
 */
function createProfileApi(user: User) {
  const updateProfile = vi.fn(async (input: { display_name: string }) => ({
    ...user,
    display_name: input.display_name,
  }));
  const uploadAvatar = vi.fn(async () => ({ avatar_url: '/avatars/after.png' }));
  return {
    api: { updateProfile, uploadAvatar } as unknown as SettingsApi,
    updateProfile,
    uploadAvatar,
  };
}

const saveButton = () => screen.getByRole('button', { name: copy.controls.save });
const cancelButton = () => screen.getByRole('button', { name: copy.controls.cancel });
const displayNameInput = () =>
  screen.getByLabelText(copy.settings.profile.displayNameLabel) as HTMLInputElement;
/** 头像的隐藏 file input：可访问名与触发按钮同文案，靠 aria-label 关联（按钮名来自文本内容）。 */
const avatarFileInput = () =>
  screen.getByLabelText(copy.settings.profile.avatarInputLabel) as HTMLInputElement;
const avatarTrigger = () =>
  screen.getByRole('button', { name: copy.settings.profile.avatarInputLabel });
const formRows = () => screen.getAllByTestId('form-row');
const rowLabelCells = () => screen.getAllByTestId('form-row-label');
/** 某行标签关联的控件 id（只读行与头像行不该有关联控件）。 */
const labelFor = (cell: HTMLElement) => cell.querySelector('label')?.getAttribute('for') ?? null;

function avatarFile(name = 'next-avatar.png'): File {
  return new File(['avatar'], name, { type: 'image/png' });
}

describe('ProfileModule', () => {
  it('only submits the edited display name and synchronizes the returned presentation value', async () => {
    const user = userEvent.setup();
    const currentUser = testUser();
    const { api, updateProfile } = createProfileApi(currentUser);
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    const input = displayNameInput();
    await user.clear(input);
    await user.type(input, '新名字');
    await user.click(saveButton());

    await waitFor(() => {
      expect(updateProfile).toHaveBeenCalledWith({ display_name: '新名字' });
    });
    expect(store.getState().user?.display_name).toBe('新名字');
    expect(input).toHaveValue('新名字');
  });

  it('uploads one avatar file and immediately updates the rendered avatar source from the response', async () => {
    const user = userEvent.setup();
    const currentUser = testUser();
    const { api, uploadAvatar } = createProfileApi(currentUser);
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    const avatar = screen.getByRole('img', { name: copy.settings.profile.avatarAlt });
    expect(avatar).toHaveAttribute('src', '/avatars/before.png');

    const file = avatarFile();
    await user.upload(avatarFileInput(), file);

    await waitFor(() => expect(uploadAvatar).toHaveBeenCalledWith(file));
    expect(avatar).toHaveAttribute('src', '/avatars/after.png');
    expect(store.getState().user?.avatar_url).toBe('/avatars/after.png');
  });

  it('姓名/部门/角色渲染为只读文本行，不渲染输入控件', async () => {
    const currentUser = testUser();
    const { api } = createProfileApi(currentUser);
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    await screen.findAllByTestId('form-row');

    for (const text of ['张三', '财务部', copy.settings.profile.roleUser]) {
      expect(screen.getByText(text)).toBeInTheDocument();
    }
    // 三行只读字段各有一条「由管理员维护」说明
    expect(screen.getAllByText(copy.settings.profile.adminManaged)).toHaveLength(3);
    // 只读行不套输入控件：全文只有显示名一个文本输入框
    expect(screen.getAllByRole('textbox')).toHaveLength(1);
    expect(screen.getAllByRole('textbox')[0]).toHaveAttribute('id', 'settings-display-name');
  });
});

describe('ProfileModule 表单结构（设置基座）', () => {
  it('五行渲染在同一张设置卡片内，页脚为卡片最后一个直接子节点', async () => {
    const currentUser = testUser();
    const { api } = createProfileApi(currentUser);
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    await screen.findByTestId('settings-card');

    expect(screen.getAllByTestId('settings-card')).toHaveLength(1);
    expect(formRows()).toHaveLength(5);
    // 行分隔线由父容器关闭末行（FormRow 始终渲染 border-b，不做「是否最后一行」判断）
    const rowsParent = formRows()[0].parentElement;
    expect(rowsParent?.className).toContain('[&>*:last-child]:border-b-0');
    expect(rowsParent?.lastElementChild).toBe(formRows()[4]);
    const card = screen.getByTestId('settings-card');
    const footer = screen.getByTestId('form-footer');
    // FormFooter 的负外边距抵消卡片 p-8，要求它是卡片的直接子节点且为最后一个子节点
    expect(card.lastElementChild).toBe(footer);
  });

  it('行标签自上而下为：头像、显示名、姓名、部门、角色', async () => {
    const currentUser = testUser();
    const { api } = createProfileApi(currentUser);
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    await screen.findAllByTestId('form-row');

    const labels = rowLabelCells().map((cell) => cell.textContent ?? '');
    expect(labels).toHaveLength(5);
    expect(labels[0]).toContain(copy.settings.profile.avatarLabel);
    expect(labels[1]).toContain(copy.settings.profile.displayNameLabel);
    expect(labels[2]).toContain(copy.settings.profile.realNameLabel);
    expect(labels[3]).toContain(copy.settings.profile.departmentLabel);
    expect(labels[4]).toContain(copy.settings.profile.roleLabel);
    // 只读行带「由管理员维护」说明，可编辑行不带
    expect(labels[1]).not.toContain(copy.settings.profile.adminManaged);
    for (const index of [2, 3, 4]) {
      expect(labels[index]).toContain(copy.settings.profile.adminManaged);
    }
  });

  it('显示名行的标签关联输入框；只读行的标签不关联任何控件', async () => {
    const currentUser = testUser();
    const { api } = createProfileApi(currentUser);
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    await screen.findAllByTestId('form-row');

    const cells = rowLabelCells();
    // 只有显示名行有可关联的控件；只读行渲染纯文本，头像行是按钮（非 labelable）
    expect(labelFor(cells[1])).toBe('settings-display-name');
    expect(labelFor(cells[0])).toBeNull();
    for (const index of [2, 3, 4]) {
      expect(labelFor(cells[index])).toBeNull();
    }
    expect(displayNameInput()).toHaveAttribute('id', 'settings-display-name');
  });
});

describe('ProfileModule 草稿-保存语义', () => {
  it('显示名改动只改草稿，点「保存」才提交', async () => {
    const user = userEvent.setup();
    const currentUser = testUser();
    const { api, updateProfile } = createProfileApi(currentUser);
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    const input = displayNameInput();
    await user.clear(input);
    await user.type(input, 'lisi');

    expect(input).toHaveValue('lisi');
    expect(updateProfile).not.toHaveBeenCalled();
    expect(store.getState().user?.display_name).toBe('张三');

    await user.click(saveButton());

    await waitFor(() => expect(updateProfile).toHaveBeenCalledWith({ display_name: 'lisi' }));
  });

  it('「取消」丢弃草稿，不产生提交请求', async () => {
    const user = userEvent.setup();
    const currentUser = testUser();
    const { api, updateProfile } = createProfileApi(currentUser);
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    const input = displayNameInput();
    await user.clear(input);
    await user.type(input, '新名字');
    await user.click(cancelButton());

    expect(input).toHaveValue('张三');
    expect(updateProfile).not.toHaveBeenCalled();
    expect(store.getState().user?.display_name).toBe('张三');
  });

  it('草稿与已提交快照一致时保存不产生请求', async () => {
    const user = userEvent.setup();
    const currentUser = testUser();
    const { api, updateProfile } = createProfileApi(currentUser);
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    await screen.findByTestId('form-footer');
    await user.click(saveButton());

    expect(updateProfile).not.toHaveBeenCalled();
  });

  it('保存进行中禁用显示名输入框与两个操作键（在途编辑不得被静默丢弃）', async () => {
    // 回归守卫：use-preferences / 保存路径的既有契约要求「保存进行中禁用可交互控件」。
    // 真实 API 的保存响应每次都是新对象（api/client.ts `response.json()`），useDraftForm 见
    // submitted 变化即重置草稿——窗口内若还能改，那笔在途编辑会在响应落地时被静默丢弃。
    const user = userEvent.setup();
    const currentUser = testUser();
    const save = deferred<User>();
    const updateProfile = vi.fn<SettingsApi['updateProfile']>(() => save.promise);
    const api = { updateProfile, uploadAvatar: vi.fn() } as unknown as SettingsApi;
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    const input = displayNameInput();
    await user.clear(input);
    await user.type(input, '新名字');
    // 先取按钮元素再点：旧实现保存中把按钮内容换成加载点、按钮失去「保存」可访问名
    const submit = saveButton();
    await user.click(submit);
    await waitFor(() => expect(submit).toBeDisabled());

    expect(input).toBeDisabled();
    expect(cancelButton()).toBeDisabled();
    // 可见禁用表现
    expect(input.className).toContain('disabled:opacity-60');
    expect(input.className).toContain('disabled:cursor-not-allowed');

    await act(async () => {
      save.resolve({ ...currentUser, display_name: '新名字' });
      await save.promise;
    });
    expect(submit).toBeEnabled();
    expect(input).toBeEnabled();
    expect(input).toHaveValue('新名字');
  });

  it('保存失败：就地错误行，按钮恢复可点，不显示「已保存」', async () => {
    const user = userEvent.setup();
    const currentUser = testUser();
    const updateProfile = vi.fn(async (_input: { display_name: string }) => {
      throw new Error('offline');
    });
    const api = { updateProfile, uploadAvatar: vi.fn() } as unknown as SettingsApi;
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    const input = displayNameInput();
    await user.clear(input);
    await user.type(input, '新名字');
    await user.click(saveButton());

    expect(await screen.findByRole('alert')).toHaveTextContent(copy.settings.profile.saveError);
    await waitFor(() => expect(saveButton()).toBeEnabled());
    expect(input).toBeEnabled();
    expect(screen.queryByText(copy.settings.profile.saved)).not.toBeInTheDocument();
    // 失败后草稿保留这次编辑（未被静默回滚成服务端快照）
    expect(input).toHaveValue('新名字');
  });

  it('保存成功：「已保存」落在页脚行内（左侧），不额外撑高页脚', async () => {
    const user = userEvent.setup();
    const currentUser = testUser();
    const { api } = createProfileApi(currentUser);
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    const footer = screen.getByTestId('form-footer');
    const footerClass = footer.className;
    const cardChildren = screen.getByTestId('settings-card').childElementCount;

    const input = displayNameInput();
    await user.clear(input);
    await user.type(input, '新名字');
    await user.click(saveButton());

    const feedback = await screen.findByText(copy.settings.profile.saved);
    // 反馈在页脚内部（与按钮同排、在左侧），而不是在页脚之上另起一行
    expect(footer).toContainElement(feedback);
    expect(feedback.closest('[data-testid="form-footer"]')).toBe(footer);
    expect(
      feedback.compareDocumentPosition(cancelButton()) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // 页脚仍是同一组高度/内边距类，卡片直接子节点数不变 → 反馈出现不改变页脚高度与纵向节奏
    expect(footer.className).toBe(footerClass);
    expect(screen.getByTestId('settings-card').childElementCount).toBe(cardChildren);
  });

  it('保存成功：显示「已保存」小字，约 2s 后淡出消失', async () => {
    const user = userEvent.setup();
    const currentUser = testUser();
    const { api } = createProfileApi(currentUser);
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    const input = displayNameInput();
    await user.clear(input);
    await user.type(input, '新名字');
    await user.click(saveButton());

    // 成功反馈随后约 2s 淡出卸载
    const feedback = await screen.findByText(copy.settings.profile.saved);
    expect(feedback).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(copy.settings.profile.saved)).not.toBeInTheDocument(), {
      timeout: 3000,
    });
  });
});

describe('ProfileModule 即时动作边界（头像）', () => {
  it('更换头像即时执行：按钮触发文件选择，选中即上传，不进入显示名草稿', async () => {
    const user = userEvent.setup();
    const currentUser = testUser();
    const { api, updateProfile, uploadAvatar } = createProfileApi(currentUser);
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    await screen.findByTestId('form-footer');

    // 触发按钮把点击转给隐藏的 file input（按钮本身是键盘可达的控件）
    const fileInput = avatarFileInput();
    const clickSpy = vi.spyOn(fileInput, 'click');
    await user.click(avatarTrigger());
    expect(clickSpy).toHaveBeenCalledTimes(1);
    clickSpy.mockRestore();

    const file = avatarFile();
    await user.upload(fileInput, file);
    await waitFor(() => expect(uploadAvatar).toHaveBeenCalledWith(file));
    // 即时动作：不等「保存」，也不经 updateProfile
    expect(updateProfile).not.toHaveBeenCalled();

    // 不使表单进入 dirty：再点「保存」不产生提交
    await user.click(saveButton());
    expect(updateProfile).not.toHaveBeenCalled();
  });

  it('更换头像不丢弃显示名的在途草稿', async () => {
    // 头像上传成功后会话用户对象整体换身份（createCurrentUserPresentationSync 以新对象落 avatar_url）。
    // 若 submitted 随 user 身份重建，useDraftForm 会把草稿重置回服务端快照，
    // 用户正在编辑的显示名会被静默丢弃。故 submitted 只随显示名取值换身份。
    const user = userEvent.setup();
    const currentUser = testUser();
    const { api, updateProfile, uploadAvatar } = createProfileApi(currentUser);
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    const input = displayNameInput();
    await user.clear(input);
    await user.type(input, '未保存名字');

    await user.upload(avatarFileInput(), avatarFile());
    await waitFor(() => expect(uploadAvatar).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(store.getState().user?.avatar_url).toBe('/avatars/after.png'));

    expect(input).toHaveValue('未保存名字');
    await user.click(saveButton());
    await waitFor(() =>
      expect(updateProfile).toHaveBeenCalledWith({ display_name: '未保存名字' }),
    );
  });

  it('头像上传后重置 input：重选同一文件可再次上传；上传中按钮显示加载反馈', async () => {
    const user = userEvent.setup();
    const currentUser = testUser();
    let resolveUpload!: () => void;
    const uploadAvatar = vi.fn(
      () =>
        new Promise<{ avatar_url: string }>((resolve) => {
          resolveUpload = () => resolve({ avatar_url: '/avatars/after.png' });
        }),
    );
    const api = { updateProfile: vi.fn(), uploadAvatar } as unknown as SettingsApi;
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    await screen.findByTestId('form-footer');
    const file = avatarFile('same-avatar.png');
    const input = avatarFileInput();
    await user.upload(input, file);
    await waitFor(() => expect(uploadAvatar).toHaveBeenCalledTimes(1));

    // 上传中：按钮文案切换为「上传中…」并禁用，file input 同步禁用
    expect(
      screen.getByRole('button', { name: copy.settings.profile.avatarUploading }),
    ).toBeDisabled();
    expect(input).toBeDisabled();

    await act(async () => {
      resolveUpload();
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: copy.settings.profile.avatarUploading }),
      ).not.toBeInTheDocument(),
    );

    // A39：onChange 后 input value 已重置 → 重选同一文件再次触发上传
    await user.upload(input, file);
    await waitFor(() => expect(uploadAvatar).toHaveBeenCalledTimes(2));
  });
});

describe('ProfileModule 关闭拦截与头像（A39）', () => {
  it('显示名有未保存更改时按 Esc 弹「放弃未保存的更改」确认；取消保留修改', async () => {
    const user = userEvent.setup();
    const currentUser = testUser();
    const { api, updateProfile } = createProfileApi(currentUser);
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    const input = displayNameInput();
    await user.clear(input);
    await user.type(input, '未保存名字');

    await user.keyboard('{Escape}');
    const dialog = await screen.findByRole('dialog', {
      name: copy.settings.profile.unsavedConfirmTitle,
    });
    expect(dialog.textContent).toContain(copy.settings.profile.unsavedConfirmDescription);

    // 取消：留在模块，修改保留
    await user.click(screen.getByRole('button', { name: copy.controls.cancel }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(displayNameInput()).toHaveValue('未保存名字');
    expect(updateProfile).not.toHaveBeenCalled();
  });

  it('确认放弃：回退显示名并关闭抽屉（导航到根路径）', async () => {
    const user = userEvent.setup();
    const currentUser = testUser();
    const { api, updateProfile } = createProfileApi(currentUser);
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    const input = displayNameInput();
    await user.clear(input);
    await user.type(input, '未保存名字');

    await user.keyboard('{Escape}');
    await screen.findByRole('dialog', { name: copy.settings.profile.unsavedConfirmTitle });
    await user.click(screen.getByRole('button', { name: copy.settings.profile.unsavedConfirm }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(displayNameInput()).toHaveValue('张三');
    expect(updateProfile).not.toHaveBeenCalled();
    expect(screen.getByTestId('profile-location').textContent).toBe('/');
  });

  it('显示名有未保存更改时点抽屉页头关闭钮弹确认，取消后不关闭（A39）', async () => {
    const user = userEvent.setup();
    const currentUser = testUser();
    const { api, updateProfile } = createProfileApi(currentUser);
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    const input = displayNameInput();
    await user.clear(input);
    await user.type(input, '未保存名字');

    // 页头关闭钮（壳层 DrawerHost 同款 aria-label）被拦截：不直接关闭，先弹确认
    await user.click(screen.getByRole('button', { name: copy.shell.drawer.closeAria }));
    const dialog = await screen.findByRole('dialog', {
      name: copy.settings.profile.unsavedConfirmTitle,
    });
    expect(dialog.textContent).toContain(copy.settings.profile.unsavedConfirmDescription);

    // 取消：留在本层，location 不变（抽屉未关闭），修改保留
    await user.click(screen.getByRole('button', { name: copy.controls.cancel }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByTestId('profile-location').textContent).toBe('/settings');
    expect(displayNameInput()).toHaveValue('未保存名字');
    expect(updateProfile).not.toHaveBeenCalled();
  });

  it('无未保存更改时按 Esc 不弹确认', async () => {
    const user = userEvent.setup();
    const currentUser = testUser();
    const { api } = createProfileApi(currentUser);
    const store = await createAuthedStore(currentUser);

    renderProfile(store, api);
    await screen.findByTestId('form-footer');
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});
