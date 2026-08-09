import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
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
import { SettingsProvider } from './SettingsProvider';
import { VersionsLayer } from './VersionsLayer';

function testUser(): User {
  return {
    id: 'u_user',
    username: 'zhangsan',
    display_name: 'zhangsan',
    real_name: 'zhangsan',
    department: { id: 'd_finance', name: '财务部' },
    role: 'user',
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
  await store.login('zhangsan', 'password123');
  return store;
}

function versionsFor(id: string) {
  return {
    document_id: id,
    version: 2,
    active_version_id: `dv_${id}_2`,
    items: [
      {
        document_version_id: `dv_${id}_2`,
        version_number: 2,
        status: 'active' as const,
        created_at: '2026-07-02T00:00:00Z',
        activated_at: '2026-07-02T00:00:00Z',
        terminal_at: null,
        superseded_at: null,
        purge_after_at: null,
        purged_at: null,
        restored_from_version_id: null,
        content_available: true,
      },
      {
        document_version_id: `dv_${id}_1`,
        version_number: 1,
        status: 'superseded' as const,
        created_at: '2026-07-01T00:00:00Z',
        activated_at: '2026-07-01T00:00:00Z',
        terminal_at: '2026-07-02T00:00:00Z',
        superseded_at: '2026-07-02T00:00:00Z',
        purge_after_at: '2099-01-01T00:00:00Z',
        purged_at: null,
        restored_from_version_id: null,
        content_available: true,
      },
    ],
  };
}

async function renderLayer(api: SettingsApi, path: readonly string[]) {
  const store = await createAuthedStore();
  let result!: ReturnType<typeof render>;
  await act(async () => {
    result = render(
      <AuthProvider store={store}>
        <MemoryRouter initialEntries={['/settings/knowledge/versions/docA']}>
          <EscStackProvider>
            <SettingsProvider
              api={api}
              authStore={store}
              theme={{ setPreference: vi.fn() } as unknown as ThemeController}
              notifications={{} as NotificationsStore}
            >
              <VersionsLayer path={path} />
            </SettingsProvider>
          </EscStackProvider>
        </MemoryRouter>
      </AuthProvider>,
    );
    await Promise.resolve();
  });
  return result;
}

describe('VersionsLayer documentId 代际（review Medium 2）', () => {
  it('恢复 A 飞行中 documentId 切到 B：A 迟到 success 不导航 uploads、不关 B 确认框', async () => {
    let resolveRestore!: () => void;
    const restoreVersion = vi.fn(() => new Promise<void>((resolve) => (resolveRestore = resolve)));
    const listVersions = vi.fn(async (documentId: string) => versionsFor(documentId));
    const api = {
      getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })),
      listVersions,
      restoreVersion,
    } as unknown as SettingsApi;
    const user = userEvent.setup();
    const result = await renderLayer(api, ['knowledge', 'versions', 'docA']);

    // A 版本已渲染 → 打开恢复确认并确认（挂起）
    expect(await screen.findByText(copy.settings.knowledge.versions.active)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.versions.restore }));
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.versions.restore }));
    await waitFor(() => expect(restoreVersion).toHaveBeenCalledTimes(1));

    // documentId A→B（真实 React rerender；effect 作废旧 operation）
    await act(async () => {
      result.rerender(
        <MemoryRouter initialEntries={['/settings/knowledge/versions/docB']}>
          <EscStackProvider>
            <SettingsProvider
              api={api}
              authStore={await createAuthedStore()}
              theme={{ setPreference: vi.fn() } as unknown as ThemeController}
              notifications={{} as NotificationsStore}
            >
              <VersionsLayer path={['knowledge', 'versions', 'docB']} />
            </SettingsProvider>
          </EscStackProvider>
        </MemoryRouter>,
      );
      await Promise.resolve();
    });
    // B 加载完成（确认框已因 documentId 变化被清）
    await waitFor(() => expect(listVersions).toHaveBeenLastCalledWith('docB'));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    // A 迟到 success：不导航 uploads、不关 B 的 dialog（已无 dialog，也不应新建）
    await act(async () => {
      resolveRestore();
      await Promise.resolve();
    });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    // 未导航（location 未变，仍在此层渲染）
    expect(screen.getByText(copy.settings.knowledge.versions.active)).toBeInTheDocument();
  });
});
