import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { AuthProvider } from '../auth/AuthProvider';
import { createMemoryAuthHub } from '../auth/channel';
import { AuthSessionStore } from '../auth/session';
import type { User } from '../auth/types';
import { copy } from '../copy';
import { EscStackProvider } from '../lib/esc-stack-provider';
import type { NotificationsStore } from '../notifications/store';
import type { ThemeController } from '../theme/theme';
import type { SettingsApi } from './api';
import { ApprovalsLayer, ManageLayer } from './ManageLayer';
import { SettingsProvider } from './SettingsProvider';

function testUser(): User {
  return {
    id: 'u_minister',
    username: 'minister-li',
    display_name: '李部长',
    real_name: '李部长',
    department: { id: 'd_finance', name: '财务部' },
    role: 'minister',
    avatar_url: null,
  };
}

async function createAuthedStore(): Promise<AuthSessionStore> {
  const user = testUser();
  const authApi = {
    login: vi.fn(async () => ({ token: 'tok_login', user })),
    logout: vi.fn(async () => {}),
    refresh: vi.fn(async () => ({ token: 'tok_refresh' })),
    me: vi.fn(async () => user),
    listSessions: vi.fn(async () => []),
    revokeSession: vi.fn(async () => {}),
    revokeAllSessions: vi.fn(async () => {}),
  };
  const store = new AuthSessionStore({ api: authApi, bus: createMemoryAuthHub().createBus() });
  await store.login('minister-li', 'password123');
  return store;
}

function createSettingsApi(): SettingsApi {
  return {
    listApprovals: vi.fn(async () => ({
      items: [
        {
          submission_id: 'sub_1',
          version: 1,
          submitter: {
            id: 'u_user',
            display_name: '张三',
            department: { id: 'd_finance', name: '财务部' },
          },
          name: '预算说明.pdf',
          media_kind: 'pdf',
          size_bytes: 1024,
          target_space_id: 'department:d_finance',
          target_space_name: '财务部',
          created_at: '2026-08-01T00:00:00Z',
        },
      ],
    })),
    rejectSubmission: vi.fn(async () => ({ submission_id: 'sub_1', version: 2, status: 'rejected' as const })),
  } as unknown as SettingsApi;
}

async function renderApprovals(api: SettingsApi) {
  const store = await createAuthedStore();
  await act(async () => {
    render(
      <AuthProvider store={store}>
        <MemoryRouter initialEntries={['/settings/knowledge/manage/approvals']}>
          <EscStackProvider>
            <SettingsProvider
              api={api}
              authStore={store}
              theme={{ setPreference: vi.fn() } as unknown as ThemeController}
              notifications={{} as NotificationsStore}
            >
              <ApprovalsLayer path={['knowledge', 'manage', 'approvals']} />
            </SettingsProvider>
          </EscStackProvider>
        </MemoryRouter>
      </AuthProvider>,
    );
    await Promise.resolve();
  });
}

/** 部门库管理层所需的三个读取接口（空间 + 待审计数 + 部门文档列表）。 */
function manageSettingsApi(): SettingsApi {
  return {
    listManageSpaces: vi.fn(async () => ({
      items: [
        {
          id: 'department:d_finance',
          kind: 'department' as const,
          name: '财务部资料库',
          permission: 'manage' as const,
          document_count: 1,
        },
      ],
    })),
    listApprovals: vi.fn(async () => ({ items: [] })),
    listDocuments: vi.fn(async () => ({
      items: [
        {
          id: 'doc_dept_1',
          document_version_id: 'dv_dept_1',
          version: 1,
          name: '部门预算.pdf',
          media_kind: 'pdf',
          version_status: 'active' as const,
          active_operation: null,
          uploaded_at: '2026-07-20T02:00:00Z',
          usage: { pages: 5, images: 1 },
        },
      ],
      total: 1,
      page: 1,
      page_size: 10,
    })),
  } as unknown as SettingsApi;
}

async function renderManage(api: SettingsApi) {
  const store = await createAuthedStore();
  await act(async () => {
    render(
      <AuthProvider store={store}>
        <MemoryRouter initialEntries={['/settings/knowledge/manage']}>
          <EscStackProvider>
            <SettingsProvider
              api={api}
              authStore={store}
              theme={{ setPreference: vi.fn() } as unknown as ThemeController}
              notifications={{} as NotificationsStore}
            >
              <ManageLayer path={['knowledge', 'manage']} />
            </SettingsProvider>
          </EscStackProvider>
        </MemoryRouter>
      </AuthProvider>,
    );
    await Promise.resolve();
  });
}

describe('ManageLayer 设置基座（抽屉视觉基座：容器）', () => {
  it('部门库管理内容位于一张设置卡片内：投稿审核入口与文档列表在卡内，卡内无小节标题', async () => {
    const api = manageSettingsApi();
    await renderManage(api);

    expect(await screen.findByText('部门预算.pdf')).toBeInTheDocument();
    const cards = screen.getAllByTestId('settings-card');
    expect(cards).toHaveLength(1);
    const card = cards[0];
    expect(card).toContainElement(
      screen.getByRole('button', { name: copy.settings.knowledge.manage.approvals }),
    );
    expect(card).toContainElement(screen.getByLabelText(copy.settings.knowledge.documents.searchAria));
    // R14：卡内不出现 h1–h6 小节标题
    expect(card.querySelectorAll('h1, h2, h3, h4, h5, h6')).toHaveLength(0);
    // 部门文档列表与个人库同构：保持自身布局，不套 FormRow
    expect(screen.queryAllByTestId('form-row')).toHaveLength(0);
  });

  it('投稿审核层内容位于一张设置卡片内；内容标题不再是卡内的 h2（R14）', async () => {
    const api = createSettingsApi();
    await renderApprovals(api);

    expect(await screen.findByText('预算说明.pdf')).toBeInTheDocument();
    const cards = screen.getAllByTestId('settings-card');
    expect(cards).toHaveLength(1);
    const card = cards[0];
    expect(card).toContainElement(screen.getByText(copy.settings.knowledge.manage.approvals));
    expect(card).toContainElement(screen.getByText('预算说明.pdf'));
    // R14：内容标题降级为普通文本（文案不变），卡内不再有 h1–h6
    expect(card.querySelectorAll('h1, h2, h3, h4, h5, h6')).toHaveLength(0);
  });
});

describe('ManageLayer 投稿驳回框', () => {
  it('打开后焦点留在框内，并可用 Enter 提交原因', async () => {
    const api = createSettingsApi();
    const user = userEvent.setup();
    await renderApprovals(api);

    await user.click(await screen.findByRole('button', { name: copy.settings.knowledge.manage.reject }));
    const dialog = await screen.findByRole('dialog', {
      name: copy.settings.knowledge.manage.rejectDialogTitle,
    });
    const input = within(dialog).getByRole('textbox');
    await waitFor(() => expect(dialog).toContainElement(document.activeElement as HTMLElement));

    input.focus();
    await user.type(input, '回车原因');
    await user.keyboard('{Enter}');

    await waitFor(() =>
      expect(api.rejectSubmission).toHaveBeenCalledWith(
        'sub_1',
        1,
        '回车原因',
        expect.stringMatching(/^idem_/),
      ),
    );
  });

  it('Tab 在框内循环，关闭后焦点恢复到打开按钮', async () => {
    const api = createSettingsApi();
    const user = userEvent.setup();
    // JSDOM 没有布局，所有元素的 offsetParent 都是 null；模拟浏览器中的可见元素。
    const offsetParent = vi.spyOn(HTMLElement.prototype, 'offsetParent', 'get').mockReturnValue(document.body);
    try {
      await renderApprovals(api);

      const trigger = await screen.findByRole('button', { name: copy.settings.knowledge.manage.reject });
      await user.click(trigger);
      const dialog = await screen.findByRole('dialog', {
        name: copy.settings.knowledge.manage.rejectDialogTitle,
      });
      const input = within(dialog).getByRole('textbox');
      const confirm = within(dialog).getByRole('button', { name: copy.settings.knowledge.manage.reject });
      await waitFor(() => expect(input).toHaveFocus());

      await user.keyboard('{Shift>}{Tab}{/Shift}');
      expect(confirm).toHaveFocus();
      await user.keyboard('{Tab}');
      expect(input).toHaveFocus();

      await user.click(within(dialog).getByRole('button', { name: copy.controls.cancel }));
      await waitFor(() => expect(trigger).toHaveFocus());
    } finally {
      offsetParent.mockRestore();
    }
  });
});
