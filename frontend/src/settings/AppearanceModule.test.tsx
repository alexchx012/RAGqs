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

/*
 * 保存回声必须还原真实 API 的对象身份行为：api/client.ts 的 `response.json()` 每次返回
 * **新对象**（`return body as T`），不是传入的那个。若 mock 直接 `async (next) => next` 回声
 * 同一引用，useDraftForm 就观察不到 submitted 变化，会掩盖「保存落地即重置草稿」的真实后果
 * （在途编辑被静默丢弃）。故统一用本函数：取值相同、身份必新。
 */
function echoPreferences(next: UserPreferences): UserPreferences {
  return { ...next };
}

function createPreferencesApi(initial: UserPreferences) {
  const getPreferences = vi.fn(async () => initial);
  const updatePreferences = vi.fn(async (next: UserPreferences) => echoPreferences(next));
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

/** 卡片渲染完成（保存键可用）即视为草稿就绪。 */
async function renderModule(api: SettingsApi, theme: ThemeController) {
  const rendered = await renderAppearance(api, theme);
  await screen.findByRole('radiogroup', { name: copy.settings.appearance.themeAria });
  return rendered;
}

const saveButton = () => screen.getByRole('button', { name: copy.controls.save });
const cancelButton = () => screen.getByRole('button', { name: copy.controls.cancel });

afterEach(() => {
  for (const controller of controllers) {
    controller.dispose();
  }
  controllers.clear();
  delete document.documentElement.dataset.chatFontSize;
});

describe('AppearanceModule', () => {
  it('loads preferences and applies theme and chat font size', async () => {
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
    // 隐私开关已挪至安全模块（共用基座 §5.4），外观模块不再渲染
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
  });

  it('places the two rows inside one settings card, footer as its direct last child', async () => {
    const { api } = createPreferencesApi(preferences());
    await renderModule(api, createThemeController());

    expect(screen.getAllByTestId('settings-card')).toHaveLength(1);
    expect(screen.getAllByTestId('form-row')).toHaveLength(2);
    const card = screen.getByTestId('settings-card');
    const footer = screen.getByTestId('form-footer');
    // FormFooter 的负外边距抵消卡片 p-8，要求它是卡片的直接子节点且为最后一个子节点
    expect(card.lastElementChild).toBe(footer);
  });

  it('行容器承载末行分隔线关闭规则，卡片直接子节点为「标题块 + 行容器 + 页脚」', async () => {
    const { api } = createPreferencesApi(preferences());
    await renderModule(api, createThemeController());

    const card = screen.getByTestId('settings-card');
    // 卡片直接子节点顺序固定：标题块 + 行容器 + FormFooter
    expect(Array.from(card.children)).toHaveLength(3);
    expect(card.children[0]).toContainElement(screen.getByRole('heading', { level: 2 }));
    expect(card.children[1].tagName).toBe('DIV');
    expect(card.children[2]).toBe(screen.getByTestId('form-footer'));

    // 末行分隔线必须由「直接包裹这组行的父容器」关闭，而不是卡片本身：
    // 卡片里 FormFooter 是最后一个子节点，加在卡片上的 :last-child 命中的会是页脚，
    // 去掉的将是页脚的下边框。FormRow 始终渲染 border-b，故这里同时确认两者。
    const rows = screen.getAllByTestId('form-row');
    const rowContainer = card.children[1];
    expect(rowContainer.className).toContain('[&>*:last-child]:border-b-0');
    expect(Array.from(rowContainer.children)).toEqual(rows);
    expect(rows[rows.length - 1].className).toContain('border-b');
    expect(card.className).not.toContain('[&>*:last-child]:border-b-0');
  });

  it('卡片在第一个表单行之前渲染模块标题与灰色副标题（措辞逐字取自设计图）', async () => {
    const { api } = createPreferencesApi(preferences());
    await renderModule(api, createThemeController());

    const card = screen.getByTestId('settings-card');
    const heading = screen.getByRole('heading', { level: 2 });
    expect(heading).toHaveTextContent('常规设置');
    expect(heading.tagName).toBe('H2');
    expect(heading.className.split(/\s+/)).toContain('text-body-lg');

    const subtitle = screen.getByText('管理界面外观与基础显示行为');
    expect(subtitle.tagName).toBe('P');
    expect(subtitle.className.split(/\s+/)).toContain('text-caption');
    expect(subtitle.className.split(/\s+/)).toContain('text-slate-strong');

    // 设计结构是「卡片标题 + 副标题 + 连续的两栏表单行」：卡内恰有 1 个标题元素，没有小节标题
    expect(card.querySelectorAll('h1, h2, h3, h4, h5, h6')).toHaveLength(1);
    const firstRow = screen.getAllByTestId('form-row')[0];
    expect(heading.compareDocumentPosition(subtitle) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(subtitle.compareDocumentPosition(firstRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('区域只在卡片上命名一次，且区域名即可见标题（无嵌套同名 landmark）', async () => {
    const { api } = createPreferencesApi(preferences());
    await renderModule(api, createThemeController());

    // 此前外层 section 与卡片同名，读屏会遇到嵌套同名 landmark（Task 8 遗留缺陷）
    const regions = screen.getAllByRole('region');
    expect(regions).toHaveLength(1);
    expect(regions[0]).toBe(screen.getByTestId('settings-card'));
    // 区域名必须等于卡内可见标题（不再取可能与标题漂移的 sectionLabel「外观」）
    const heading = screen.getByRole('heading', { level: 2 });
    expect(regions[0]).toHaveAccessibleName(heading.textContent ?? '');
    expect(regions[0]).toHaveAccessibleName(copy.settings.appearance.cardTitle);
    expect(regions[0]).not.toHaveAttribute('aria-label');
    // 标题元素自身不挂 aria-label（同一元素上标题 + aria-label 会被重复朗读）
    expect(heading).not.toHaveAttribute('aria-label');
  });

  it('只渲染主题与对话字号两行（界面语言/消息时间戳不在本期范围）', async () => {
    const { api } = createPreferencesApi(preferences());
    await renderModule(api, createThemeController());

    const labels = screen.getAllByTestId('form-row-label').map((cell) => cell.textContent ?? '');
    expect(labels).toHaveLength(2);
    expect(labels[0]).toContain(copy.settings.appearance.themeTitle);
    expect(labels[1]).toContain(copy.settings.appearance.fontSizeTitle);
  });

  it('分段控件改动只改草稿，点保存才写偏好', async () => {
    const { api, updatePreferences } = createPreferencesApi(preferences());
    const theme = createThemeController();
    const user = userEvent.setup();

    await renderModule(api, theme);
    await user.click(screen.getByRole('radio', { name: copy.settings.appearance.themeDark }));

    // 草稿即时反映选择，但偏好未被写入
    expect(screen.getByRole('radio', { name: copy.settings.appearance.themeDark })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(updatePreferences).not.toHaveBeenCalled();
    expect(theme.getPreference()).toBe('system');

    await user.click(saveButton());

    await waitFor(() =>
      expect(updatePreferences).toHaveBeenCalledWith({
        theme: 'dark',
        chat_font_size: 'standard',
        ab_opt_out: false,
      }),
    );
    expect(theme.getPreference()).toBe('dark');
  });

  it('保存提交完整快照，含未在本模块渲染的字段', async () => {
    const { api, updatePreferences } = createPreferencesApi(preferences({ ab_opt_out: true }));
    const user = userEvent.setup();

    await renderModule(api, createThemeController());
    await user.click(screen.getByRole('radio', { name: copy.settings.appearance.fontLarge }));
    await user.click(saveButton());

    await waitFor(() => expect(updatePreferences).toHaveBeenCalledTimes(1));
    expect(updatePreferences).toHaveBeenCalledWith({
      theme: 'system',
      chat_font_size: 'large',
      ab_opt_out: true,
    });
    expect(document.documentElement.dataset.chatFontSize).toBe('large');
  });

  it('取消丢弃草稿，不写偏好', async () => {
    const { api, updatePreferences } = createPreferencesApi(preferences());
    const theme = createThemeController();
    const user = userEvent.setup();

    await renderModule(api, theme);
    await user.click(screen.getByRole('radio', { name: copy.settings.appearance.themeDark }));
    await user.click(cancelButton());

    expect(updatePreferences).not.toHaveBeenCalled();
    expect(
      screen.getByRole('radio', { name: copy.settings.appearance.themeSystem }),
    ).toHaveAttribute('aria-checked', 'true');
    expect(theme.getPreference()).toBe('system');
  });

  it('草稿与已提交快照一致时保存不产生请求', async () => {
    const { api, updatePreferences } = createPreferencesApi(preferences());
    const user = userEvent.setup();

    await renderModule(api, createThemeController());
    await user.click(saveButton());

    expect(updatePreferences).not.toHaveBeenCalled();
  });

  it('保存进行中禁用两个操作键', async () => {
    const save = deferred<UserPreferences>();
    const getPreferences = vi.fn(async () => preferences());
    const updatePreferences = vi.fn<SettingsApi['updatePreferences']>(() => save.promise);
    const api = { getPreferences, updatePreferences } as unknown as SettingsApi;
    const user = userEvent.setup();

    await renderModule(api, createThemeController());
    await user.click(screen.getByRole('radio', { name: copy.settings.appearance.themeDark }));
    await user.click(saveButton());

    await waitFor(() => expect(saveButton()).toBeDisabled());
    expect(cancelButton()).toBeDisabled();

    await act(async () => {
      save.resolve(preferences({ theme: 'dark' }));
      await save.promise;
    });
    expect(saveButton()).toBeEnabled();
  });

  it('保存进行中分段控件不可交互（在途编辑不得被静默丢弃）', async () => {
    // 回归守卫：真实 API 的保存响应每次都是新对象（api/client.ts `response.json()`），
    // useDraftForm 见 submitted 身份变化即重置草稿。若保存窗口内控件仍可改，
    // 那笔在途编辑会在响应落地时被重置掉，且再点保存是 no-op（草稿已等于服务端快照）——
    // 编辑无声消失。use-preferences 对 saving 的既有契约要求消费方禁用相关控件。
    const save = deferred<UserPreferences>();
    const getPreferences = vi.fn(async () => preferences());
    const updatePreferences = vi.fn<SettingsApi['updatePreferences']>(() => save.promise);
    const api = { getPreferences, updatePreferences } as unknown as SettingsApi;
    const theme = createThemeController();
    const user = userEvent.setup();

    await renderModule(api, theme);
    await user.click(screen.getByRole('radio', { name: copy.settings.appearance.themeDark }));
    await user.click(saveButton());
    await waitFor(() => expect(saveButton()).toBeDisabled());

    expect(screen.getByRole('radio', { name: copy.settings.appearance.themeDark })).toBeDisabled();
    expect(screen.getByRole('radio', { name: copy.settings.appearance.fontLarge })).toBeDisabled();
    expect(screen.getByRole('radio', { name: copy.settings.appearance.fontLarge })).toBeDisabled();

    // 在途点击被吞掉：草稿仍是提交前那次选择
    await user.click(screen.getByRole('radio', { name: copy.settings.appearance.themeLight }));
    expect(screen.getByRole('radio', { name: copy.settings.appearance.themeDark })).toHaveAttribute(
      'aria-checked',
      'true',
    );

    // 保存落地后（新对象身份 → 草稿重置为服务端快照）用户此前的选择仍被保留
    await act(async () => {
      save.resolve(echoPreferences(preferences({ theme: 'dark' })));
      await save.promise;
    });
    expect(theme.getPreference()).toBe('dark');
    expect(screen.getByRole('radio', { name: copy.settings.appearance.themeDark })).toHaveAttribute(
      'aria-checked',
      'true',
    );

    // 另一条静默丢失路径已不可达：草稿不可能在响应落地后被重置成「与快照不同」，
    // 因此不存在「用户以为还有未保存改动、再点保存却 no-op」的状态。
    expect(screen.getByRole('radio', { name: copy.settings.appearance.fontStandard })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    await user.click(saveButton());
    expect(updatePreferences).toHaveBeenCalledTimes(1);
  });

  it('保存进行中的禁用态可见（降透明度 + 不可点光标）', async () => {
    const save = deferred<UserPreferences>();
    const getPreferences = vi.fn(async () => preferences());
    const updatePreferences = vi.fn<SettingsApi['updatePreferences']>(() => save.promise);
    const api = { getPreferences, updatePreferences } as unknown as SettingsApi;
    const user = userEvent.setup();

    await renderModule(api, createThemeController());
    const group = screen.getByRole('radiogroup', { name: copy.settings.appearance.themeAria });
    // 保存前不处于禁用态：断言真实属性而不是某个类名（fieldset 的 opacity 初始值即 1，
    // 原先钉住 enabled:opacity-100 只是锁住一条 no-op 类）
    expect(group.closest('fieldset')).not.toBeDisabled();

    await user.click(screen.getByRole('radio', { name: copy.settings.appearance.themeDark }));
    await user.click(saveButton());
    await waitFor(() => expect(saveButton()).toBeDisabled());

    const disabledFieldset = group.closest('fieldset');
    expect(disabledFieldset).not.toBeNull();
    expect(disabledFieldset?.className).toContain('opacity-60');
    expect(disabledFieldset?.className).toContain('disabled:cursor-not-allowed');
  });

  it('保存失败时回滚运行时并显示可读错误', async () => {
    const initial = preferences();
    const { api, updatePreferences } = createPreferencesApi(initial);
    updatePreferences.mockRejectedValueOnce(new Error('offline'));
    const theme = createThemeController();
    const user = userEvent.setup();

    await renderModule(api, theme);
    await user.click(screen.getByRole('radio', { name: copy.settings.appearance.themeDark }));
    await user.click(saveButton());

    expect(await screen.findByRole('alert')).toHaveTextContent(copy.settings.appearance.saveError);
    expect(theme.getPreference()).toBe('system');
    expect(document.documentElement.dataset.chatFontSize).toBe('standard');
    expect(
      screen.getByRole('radio', { name: copy.settings.appearance.themeSystem }),
    ).toHaveAttribute('aria-checked', 'true');
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

  it('加载完成前不渲染草稿表单', async () => {
    const pending = deferred<UserPreferences>();
    const getPreferences = vi.fn<SettingsApi['getPreferences']>(() => pending.promise);
    const api = {
      getPreferences,
      updatePreferences: vi.fn(async (next: UserPreferences) => echoPreferences(next)),
    } as unknown as SettingsApi;
    const theme = createThemeController();

    await renderAppearance(api, theme);
    await waitFor(() => expect(getPreferences).toHaveBeenCalledTimes(1));

    expect(screen.getByRole('status')).toHaveTextContent(copy.settings.appearance.loading);
    expect(screen.queryByTestId('settings-card')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: copy.controls.save })).not.toBeInTheDocument();

    await act(async () => {
      pending.resolve(preferences());
      await pending.promise;
    });
    expect(await screen.findByTestId('settings-card')).toBeInTheDocument();
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
      updatePreferences: vi.fn(async (next: UserPreferences) => echoPreferences(next)),
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
      .mockResolvedValueOnce(echoPreferences(initial))
      .mockResolvedValueOnce(echoPreferences(nextSession));
    const updatePreferences = vi.fn<SettingsApi['updatePreferences']>(() => save.promise);
    const api = { getPreferences, updatePreferences } as unknown as SettingsApi;
    const theme = createThemeController();
    const user = userEvent.setup();

    const { store } = await renderModule(api, theme);
    await user.click(screen.getByRole('radio', { name: copy.settings.appearance.themeDark }));
    await user.click(saveButton());
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

    const { result } = await renderModule(api, theme);
    await user.click(screen.getByRole('radio', { name: copy.settings.appearance.themeDark }));
    await user.click(saveButton());
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

    const { result } = await renderModule(api, theme);
    await user.click(screen.getByRole('radio', { name: copy.settings.appearance.themeDark }));
    await user.click(saveButton());
    await waitFor(() => expect(updatePreferences).toHaveBeenCalledTimes(1));
    expect(theme.getPreference()).toBe('dark');
    result.unmount();

    await act(async () => {
      save.reject(new Error('offline'));
      await save.promise.catch(() => undefined);
    });
    expect(theme.getPreference()).toBe('system');
    expect(document.documentElement.dataset.chatFontSize).toBe('standard');
  });
});
