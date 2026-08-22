/*
 * 聊天主页集成测试（fe-chat-home 替换占位后，保留 shared-shell 行为）：
 * - 侧边栏头像区按角色打开抽屉（个人段「设置」/ 管理段「总览」）；
 * - 运维登录落地标记自动展开管理段首层；
 * - 抽屉开关心路中主页保持挂载（输入草稿原样保留）；
 * - 主页右上角铃铛；
 * - 真实 ChatStore 经 MSW 契约 mock 装配：登录 mint 有效 Bearer 供 chat API 使用。
 */

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useLocation } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { NotificationsStore } from '../notifications/store';
import { AppRoutes } from '../router/AppRoutes';
import { AUTO_OPEN_ADMIN_DRAWER_STATE_KEY } from '../router/landing';
import { mockAuth, mockChat } from '../mocks/testing';
import {
  createTestStore,
  fakeAuthApi,
  fakeNotificationsApi,
  renderWithShell,
  testUser,
} from '../test/auth-fixtures';
import type { User } from '../auth/types';

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location-path">{location.pathname}</output>;
}

/** 经 mockAuth 铸造真实 Bearer：ChatStore 的 chat API 调用才能通过 MSW 契约 mock。 */
async function createAuthedChatStore(user: User = testUser()) {
  const { accessToken } = mockAuth.login(user.username, 'password123', 'vitest');
  const store = createTestStore(
    fakeAuthApi({
      login: vi.fn(async () => ({ token: accessToken, user })),
      refresh: vi.fn(async () => ({ token: accessToken })),
      me: vi.fn(async () => user),
    }),
  );
  await store.bootstrap();
  return store;
}

async function createAuthedEmptyChatStore(user: User = testUser()) {
  const { accessToken } = mockAuth.login(user.username, 'password123', 'vitest');
  mockChat.deleteConversation(`Bearer ${accessToken}`, 'c_1');
  mockChat.deleteConversation(`Bearer ${accessToken}`, 'c_ab');
  const store = createTestStore(
    fakeAuthApi({
      login: vi.fn(async () => ({ token: accessToken, user })),
      refresh: vi.fn(async () => ({ token: accessToken })),
      me: vi.fn(async () => user),
    }),
  );
  await store.bootstrap();
  return { store, accessToken };
}

/** 聊天主页落地信号：输入区 composer（会话恢复/空态问候语随数据异步出现，composer 恒在）。 */
const landingComposer = () => screen.findByRole('textbox', { name: copy.chat.composer.inputPlaceholder });

describe('聊天主页（fe-chat-home 集成）', () => {
  it('普通用户点头像区 → 打开个人段「设置」抽屉', async () => {
    const store = await createAuthedChatStore();
    renderWithShell(<AppRoutes />, store, ['/']);
    const user = userEvent.setup();
    await landingComposer();
    await user.click(
      screen.getAllByRole('button', { name: copy.shell.home.openDrawerAria })[0] as HTMLElement,
    );
    expect(
      await screen.findByRole('dialog', { name: copy.shell.drawer.personalTitle }),
    ).toBeInTheDocument();
  });

  it('运维点头像区 → 打开管理段「总览」（共用基座 §3.2）', async () => {
    const store = await createAuthedChatStore(testUser({ role: 'ops', username: 'ops-wang' }));
    renderWithShell(<AppRoutes />, store, ['/']);
    const user = userEvent.setup();
    await landingComposer();
    await user.click(
      screen.getAllByRole('button', { name: copy.shell.home.openDrawerAria })[0] as HTMLElement,
    );
    expect(
      await screen.findByRole('dialog', { name: copy.shell.drawer.modules.dashboard }),
    ).toBeInTheDocument();
  });

  it('运维登录落地标记驱动抽屉自动展开到管理段首层（规格 §1）', async () => {
    const store = await createAuthedChatStore(testUser({ role: 'ops', username: 'ops-wang' }));
    renderWithShell(
      <>
        <AppRoutes />
        <LocationProbe />
      </>,
      store,
      [{ pathname: '/', state: { [AUTO_OPEN_ADMIN_DRAWER_STATE_KEY]: true } }],
    );
    expect(
      await screen.findByRole('dialog', { name: copy.shell.drawer.modules.dashboard }),
    ).toBeInTheDocument();
    // replace 消费标记后 URL 为 /admin/dashboard
    await waitFor(() =>
      expect(screen.getByTestId('location-path').textContent).toBe('/admin/dashboard'),
    );
  });

  it('抽屉开关心路中主页保持挂载：输入草稿原样保留（共用基座 §5.1）', async () => {
    const store = await createAuthedChatStore();
    renderWithShell(<AppRoutes />, store, ['/']);
    const user = userEvent.setup();
    const composer = await landingComposer();
    await user.type(composer, '报销流程怎么走');
    await user.click(
      screen.getAllByRole('button', { name: copy.shell.home.openDrawerAria })[0] as HTMLElement,
    );
    const dialog = await screen.findByRole('dialog', { name: copy.shell.drawer.personalTitle });
    // 抽屉打开期间主页仍在下方挂载
    expect(composer).toHaveValue('报销流程怎么走');
    await user.click(within(dialog).getByRole('button', { name: copy.shell.drawer.closeAria }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument(), {
      timeout: 2000,
    });
    expect(
      screen.getByRole('textbox', { name: copy.chat.composer.inputPlaceholder }),
    ).toHaveValue('报销流程怎么走');
  });

  it('主页右上角挂铃铛（共用基座 §3.1）', async () => {
    const store = await createAuthedChatStore();
    renderWithShell(<AppRoutes />, store, ['/']);
    await landingComposer();
    expect(
      screen.getByRole('button', { name: copy.notifications.bellAria }),
    ).toBeInTheDocument();
  });

  it('主页渲染侧边栏会话列表与消息（经 MSW 契约 mock 真实装配）', async () => {
    const store = await createAuthedChatStore();
    renderWithShell(<AppRoutes />, store, ['/']);
    // 种子 c_1 消息出现（自动打开最近会话）
    const seeded = await screen.findByText('Mock seeded answer about annual leave.');
    expect(seeded).toBeInTheDocument();
    // 侧边栏搜索框与新建会话按钮在场（桌面 + 窄屏抽屉双份）
    expect(
      screen.getAllByRole('searchbox', { name: copy.chat.sidebar.searchPlaceholder }).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByRole('button', { name: copy.chat.sidebar.newConversation }).length,
    ).toBeGreaterThan(0);
  });

  it('无当前会话/空列表时发送首问：先创建会话再发送，不吞输入', async () => {
    const { store, accessToken } = await createAuthedEmptyChatStore();
    renderWithShell(<AppRoutes />, store, ['/']);
    const user = userEvent.setup();
    const composer = await landingComposer();

    await user.type(composer, '空会话首问');
    await user.click(screen.getByRole('button', { name: copy.chat.composer.sendAria }));
    await waitFor(() => expect(composer).toHaveValue(''));
    await waitFor(() => {
      const created = mockChat
        .listConversations(`Bearer ${accessToken}`)
        .items.find((item) => item.title === '空会话首问');
      expect(created).toBeDefined();
      const detail = mockChat.getConversation(`Bearer ${accessToken}`, created?.id ?? '');
      expect(detail.messages.some((message) => message.role === 'user' && message.content === '空会话首问')).toBe(true);
      expect(detail.messages.some((message) => message.role === 'assistant' && message.status === 'completed')).toBe(true);
    });
  });

  it('已处于新会话界面再点「新建会话」不重复创建（侧栏不堆叠空会话）', async () => {
    const account = testUser();
    const { accessToken } = mockAuth.login(account.username, 'password123', 'vitest');
    const store = createTestStore(
      fakeAuthApi({
        login: vi.fn(async () => ({ token: accessToken, user: account })),
        refresh: vi.fn(async () => ({ token: accessToken })),
        me: vi.fn(async () => account),
      }),
    );
    await store.bootstrap();
    renderWithShell(<AppRoutes />, store, ['/']);
    const user = userEvent.setup();
    await landingComposer();

    const bearer = `Bearer ${accessToken}`;
    const freshItems = () =>
      mockChat.listConversations(bearer).items.filter((item) => item.title === '');
    const newButton = () =>
      screen.getAllByRole('button', { name: copy.chat.sidebar.newConversation })[0] as HTMLElement;

    await user.click(newButton());
    await waitFor(() => expect(freshItems()).toHaveLength(1));
    const freshId = freshItems()[0]?.id;

    // 已停留在新会话界面：再点不创建第二个空会话
    await user.click(newButton());
    await user.click(newButton());
    // 给潜在的重复创建请求留出发出窗口（若有回归，这里会出现第二条空会话）
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(freshItems().map((item) => item.id)).toEqual([freshId]);
  });

  it('在旧会话界面点「新建会话」：指向已有新会话而非再建', async () => {
    const account = testUser();
    const { accessToken } = mockAuth.login(account.username, 'password123', 'vitest');
    const store = createTestStore(
      fakeAuthApi({
        login: vi.fn(async () => ({ token: accessToken, user: account })),
        refresh: vi.fn(async () => ({ token: accessToken })),
        me: vi.fn(async () => account),
      }),
    );
    await store.bootstrap();
    renderWithShell(<AppRoutes />, store, ['/']);
    const user = userEvent.setup();
    await landingComposer();

    const bearer = `Bearer ${accessToken}`;
    const freshItems = () =>
      mockChat.listConversations(bearer).items.filter((item) => item.title === '');
    const newButton = () =>
      screen.getAllByRole('button', { name: copy.chat.sidebar.newConversation })[0] as HTMLElement;
    const greetingHidden = () =>
      document.querySelector('.chat-empty-greeting')?.getAttribute('data-hidden');

    // 先建一个新会话（停留在新会话界面）
    await user.click(newButton());
    await waitFor(() => expect(freshItems()).toHaveLength(1));
    const freshId = freshItems()[0]?.id;
    await waitFor(() => expect(greetingHidden()).toBe('false'));

    // 打开一条旧会话：离开新会话界面
    await user.click(
      screen.getAllByRole('button', { name: /年假怎么休/ })[0] as HTMLElement,
    );
    await waitFor(() => expect(greetingHidden()).toBe('true'));

    // 再点「新建会话」：指向原来的新会话（数量不变，回到新会话界面）
    await user.click(newButton());
    await waitFor(() => expect(greetingHidden()).toBe('false'));
    expect(freshItems().map((item) => item.id)).toEqual([freshId]);
  });

  it('新登录落地：无新会话时自动创建并进入新会话界面', async () => {
    const account = testUser();
    const { accessToken } = mockAuth.login(account.username, 'password123', 'vitest');
    const store = createTestStore(
      fakeAuthApi({
        login: vi.fn(async () => ({ token: accessToken, user: account })),
        refresh: vi.fn(async () => ({ token: accessToken })),
        me: vi.fn(async () => account),
      }),
    );
    // 交互式 login 才置位新登录落地标记（AuthSessionStore 一次性标记，不经导航 state）
    await store.login(account.username, 'password123');
    renderWithShell(<AppRoutes />, store, ['/']);
    await landingComposer();

    await waitFor(() => {
      const fresh = mockChat
        .listConversations(`Bearer ${accessToken}`)
        .items.filter((item) => item.title === '');
      expect(fresh).toHaveLength(1);
    });
    await waitFor(() => {
      expect(document.querySelector('.chat-empty-greeting')?.getAttribute('data-hidden')).toBe(
        'false',
      );
    });
  });

  it('新登录落地：已有新会话则指向它，不重复创建', async () => {
    const account = testUser();
    const { accessToken } = mockAuth.login(account.username, 'password123', 'vitest');
    const bearer = `Bearer ${accessToken}`;
    const pre = mockChat.createConversation(bearer); // 预置一个未命名新会话
    const store = createTestStore(
      fakeAuthApi({
        login: vi.fn(async () => ({ token: accessToken, user: account })),
        refresh: vi.fn(async () => ({ token: accessToken })),
        me: vi.fn(async () => account),
      }),
    );
    await store.login(account.username, 'password123');
    renderWithShell(<AppRoutes />, store, ['/']);
    await landingComposer();

    await waitFor(() => {
      const fresh = mockChat
        .listConversations(bearer)
        .items.filter((item) => item.title === '');
      expect(fresh.map((item) => item.id)).toEqual([pre.id]);
    });
    await waitFor(() => {
      expect(document.querySelector('.chat-empty-greeting')?.getAttribute('data-hidden')).toBe(
        'false',
      );
    });
  });

  it('检索范围 chip：文档名过滤 q 经 HomePage 透传到 listDocuments（m4 集成）', async () => {
    const store = await createAuthedChatStore();
    renderWithShell(<AppRoutes />, store, ['/']);
    const user = userEvent.setup();
    await landingComposer();
    // 展开检索范围
    await user.click(screen.getByRole('button', { name: copy.chat.composer.scopeAria }));
    // 下钻个人库文档
    await user.click(await screen.findByRole('button', { name: copy.chat.composer.scopeDocumentDrillAria }));
    // 初始列表含员工手册 / 报销制度
    expect(await screen.findByText('员工手册.pdf')).toBeInTheDocument();
    expect(screen.getByText('报销制度.docx')).toBeInTheDocument();
    // 文档搜索框输入 q：HomePage fetchDocuments 必须透传第二参，mock 按名过滤
    const docSearch = screen.getByRole('searchbox', {
      name: copy.chat.composer.scopeDocumentSearchPlaceholder,
    });
    await user.type(docSearch, '员工');
    await waitFor(() => {
      expect(screen.getByText('员工手册.pdf')).toBeInTheDocument();
      expect(screen.queryByText('报销制度.docx')).not.toBeInTheDocument();
    });
  });
});

describe('通知轮询生命周期（规格 §4：仅已认证时运行）', () => {
  it('认证壳层挂载即启动轮询（立即拉一次未读数），卸载停止', async () => {
    const unreadCount = vi.fn(async () => ({ count: 3 }));
    const notifications = new NotificationsStore(fakeNotificationsApi({ unreadCount }));
    const store = await createAuthedChatStore();
    const { unmount } = renderWithShell(<AppRoutes />, store, ['/'], { notifications });
    await waitFor(() => expect(notifications.getState().unreadCount).toBe(3));
    expect(unreadCount).toHaveBeenCalledTimes(1);
    unmount();
    expect(notifications.getState().unreadCount).toBeNull();
  });
});
