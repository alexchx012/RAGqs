import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
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
    display_name: 'minister-li',
    real_name: 'minister-li',
    department: { id: 'd_finance', name: '财务部' },
    role: 'minister',
    avatar_url: null,
  };
}

async function createAuthedStore(): Promise<AuthSessionStore> {
  const user = testUser();
  const api = {
    login: vi.fn(async () => ({ token: 'tok_login', user })),
    logout: vi.fn(async () => {}),
    refresh: vi.fn(async () => ({ token: 'tok_refresh' })),
    me: vi.fn(async () => user),
    listSessions: vi.fn(async () => []),
    revokeSession: vi.fn(async () => {}),
    revokeAllSessions: vi.fn(async () => {}),
  };
  const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
  await store.login('minister-li', 'password123');
  return store;
}

async function renderLayer(api: SettingsApi) {
  const store = await createAuthedStore();
  await act(async () => {
    render(
      <AuthProvider store={store}>
        <MemoryRouter initialEntries={['/']}>
          <EscStackProvider>
            <SettingsProvider
              api={api}
              authStore={store}
              theme={{ setPreference: vi.fn() } as unknown as ThemeController}
              notifications={{} as NotificationsStore}
            >
              <ApprovalsLayer path={[]} />
            </SettingsProvider>
          </EscStackProvider>
        </MemoryRouter>
      </AuthProvider>,
    );
    await Promise.resolve();
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('ApprovalsLayer submission content download', () => {
  it('downloads a pending original without opening a Blob preview window', async () => {
    const api = {
      getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })),
      listApprovals: vi.fn(async () => ({
        items: [
          {
            submission_id: 'submission_approval_1',
            version: 1,
            submitter: {
              id: 'u_submitter',
              display_name: 'submitter',
              department: { id: 'd_finance', name: '财务部' },
            },
            name: 'unsafe.svg',
            media_kind: 'image/svg+xml',
            size_bytes: 24,
            target_space_id: 'department:finance',
            target_space_name: '财务部知识库',
            created_at: '2026-08-15T00:00:00Z',
          },
        ],
      })),
      getApprovalSummary: vi.fn(async () => ({ quota_pending: 0, submission_pending: 1 })),
      getSubmissionContent: vi.fn(async () => new Blob(['<svg />'], { type: 'image/svg+xml' })),
    } as unknown as SettingsApi;
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);
    const createSpy = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:approval-download');
    const revokeSpy = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    const downloads: { href: string; filename: string }[] = [];
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function (this: HTMLAnchorElement) {
        downloads.push({ href: this.href, filename: this.download });
      });
    const user = userEvent.setup();

    await renderLayer(api);
    await user.click(await screen.findByRole('button', { name: copy.settings.knowledge.manage.viewContent }));

    await waitFor(() =>
      expect(downloads).toEqual([{ href: 'blob:approval-download', filename: 'unsafe.svg' }]),
    );
    expect(openSpy).not.toHaveBeenCalled();
    expect(createSpy).toHaveBeenCalledTimes(1);
    expect(revokeSpy).toHaveBeenCalledWith('blob:approval-download');
    clickSpy.mockRestore();
  });
});
