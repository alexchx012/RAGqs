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
import { ApprovalsLayer } from './ManageLayer';
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
          space_id: 'department:d_finance',
          version: 1,
          status: 'pending' as const,
          file_name: '预算说明.pdf',
          media_kind: 'pdf',
          submitter_name: '张三',
          submitter_department: { id: 'd_finance', name: '财务部' },
          file_size: 1024,
          created_at: '2026-08-01T00:00:00Z',
          reviewed_at: null,
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
