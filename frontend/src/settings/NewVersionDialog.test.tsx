import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import { AuthProvider } from '../auth/AuthProvider';
import { createMemoryAuthHub } from '../auth/channel';
import { AuthSessionStore } from '../auth/session';
import type { User } from '../auth/types';
import { ApiError } from '../api/errors';
import { copy } from '../copy';
import { EscStackProvider } from '../lib/esc-stack-provider';
import { mockAuth, mockKnowledge } from '../mocks/testing';
import type { NotificationsStore } from '../notifications/store';
import type { ThemeController } from '../theme/theme';
import type { SettingsApi } from './api';
import { SettingsProvider } from './SettingsProvider';
import { NewVersionDialog } from './NewVersionDialog';
import type { DocumentListItem } from './types';
import { clearUploadHistory } from './upload-history';

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

function createContractApi(): SettingsApi {
  const { accessToken } = mockAuth.login('zhangsan', 'password123', 'vitest');
  const token = `Bearer ${accessToken}`;
  return {
    getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })),
    uploadNewVersion: vi.fn(async (documentId, file, expectedVersion, idem) => {
      const parsed = { name: file.name, size: file.size, type: file.type };
      return mockKnowledge.uploadNewVersion(token, documentId, parsed, expectedVersion, idem);
    }),
    listJobs: vi.fn(async () => ({ items: [], limit: 50, max_limit: 200, has_more: false })),
  } as unknown as SettingsApi;
}

const TARGET: DocumentListItem = {
  id: 'doc_personal_u_user_1',
  document_version_id: 'dv_1',
  version: 1,
  name: '员工手册.pdf',
  media_kind: 'pdf',
  version_status: 'active',
  active_operation: null,
  uploaded_at: '2026-07-20T02:00:00Z',
  usage: { pages: 50, images: 40 },
};

async function renderDialog(api: SettingsApi, onSubmitted: () => void) {
  const store = await createAuthedStore();
  let result!: ReturnType<typeof render>;
  await act(async () => {
    result = render(
      <AuthProvider store={store}>
        <MemoryRouter initialEntries={['/settings/knowledge']}>
          <EscStackProvider>
            <SettingsProvider
              api={api}
              authStore={store}
              theme={{ setPreference: vi.fn() } as unknown as ThemeController}
              notifications={{} as NotificationsStore}
            >
              <NewVersionDialog target={TARGET} onClose={() => {}} onSubmitted={onSubmitted} />
            </SettingsProvider>
          </EscStackProvider>
        </MemoryRouter>
      </AuthProvider>,
    );
    await Promise.resolve();
  });
  return result;
}

afterEach(() => {
  mockKnowledge.reset();
  mockAuth.reset();
  clearUploadHistory();
});

describe('NewVersionDialog 上传新版本（§6.4）', () => {
  it('固定目标 document_id + expected_version，单文件；成功后 onSubmitted', async () => {
    const api = createContractApi();
    const onSubmitted = vi.fn();
    const user = userEvent.setup();
    await renderDialog(api, onSubmitted);

    expect(screen.getByRole('dialog', { name: copy.settings.knowledge.upload.newVersionDialogTitle })).toBeInTheDocument();
    expect(
      screen.getByText(copy.settings.knowledge.upload.newVersionDescription(TARGET.name)),
    ).toBeInTheDocument();

    const file = new File(['%PDF-1.4'], '员工手册-v2.pdf', { type: 'application/pdf' });
    await user.upload(screen.getByLabelText(copy.settings.knowledge.upload.chooseFiles), file);
    await user.click(screen.getByRole('button', { name: copy.controls.confirm }));

    await waitFor(() => expect(onSubmitted).toHaveBeenCalledTimes(1));
    const call = api.uploadNewVersion as ReturnType<typeof vi.fn>;
    expect(call).toHaveBeenCalledWith(
      TARGET.id,
      expect.any(File),
      TARGET.version,
      expect.stringMatching(/^idem_/),
    );
  });

  it('网络未知重试复用同键（第二次调用同 Idempotency-Key）', async () => {
    const api = createContractApi();
    const onSubmitted = vi.fn();
    const user = userEvent.setup();
    // 首次失败（网络未知：无业务响应）→ 保持同键；第二次成功
    let attempts = 0;
    const uploadNewVersion = vi.fn(async (_documentId: string, _file: File, _expectedVersion: number, idem: string) => {
      attempts += 1;
      if (attempts === 1) {
        throw new Error('network offline');
      }
      const { accessToken } = mockAuth.login('zhangsan', 'password123', 'retry-device');
      const parsed = { name: '员工手册-v2.pdf', size: 8, type: 'application/pdf' };
      return mockKnowledge.uploadNewVersion(`Bearer ${accessToken}`, 'doc_personal_u_user_1', parsed, 1, idem);
    });
    const api2 = {
      getPreferences: api.getPreferences,
      uploadNewVersion,
    } as unknown as SettingsApi;
    await renderDialog(api2, onSubmitted);

    const file = new File(['%PDF-1.4'], '员工手册-v2.pdf', { type: 'application/pdf' });
    await user.upload(screen.getByLabelText(copy.settings.knowledge.upload.chooseFiles), file);
    await user.click(screen.getByRole('button', { name: copy.controls.confirm }));
    // 网络未知失败：不换键；等待首次调用完成后再次点击重试
    await waitFor(() => expect(uploadNewVersion).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole('button', { name: copy.controls.confirm }));

    await waitFor(() => expect(uploadNewVersion).toHaveBeenCalledTimes(2));
    const firstKey = uploadNewVersion.mock.calls[0]?.[3];
    const secondKey = uploadNewVersion.mock.calls[1]?.[3];
    expect(firstKey).toBe(secondKey);
    await waitFor(() => expect(onSubmitted).toHaveBeenCalledTimes(1));
  });
});

describe('NewVersionDialog completion 隔离（review A3）', () => {
  it('提交中 Esc 关闭（token 失效）：迟到成功不触发 onSubmitted/onConflictRefresh', async () => {
    let resolveUpload!: (value: unknown) => void;
    const uploadNewVersion = vi.fn(() => new Promise((resolve) => (resolveUpload = resolve)));
    const onSubmitted = vi.fn();
    const onConflictRefresh = vi.fn();
    const api = {
      getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })),
      uploadNewVersion,
    } as unknown as SettingsApi;
    const user = userEvent.setup();

    await act(async () => {
      const store = await createAuthedStore();
      render(
        <AuthProvider store={store}>
          <MemoryRouter initialEntries={['/settings/knowledge']}>
            <EscStackProvider>
              <SettingsProvider
                api={api}
                authStore={store}
                theme={{ setPreference: vi.fn() } as unknown as ThemeController}
                notifications={{} as NotificationsStore}
              >
                <NewVersionDialog target={TARGET} onClose={() => {}} onSubmitted={onSubmitted} onConflictRefresh={onConflictRefresh} />
              </SettingsProvider>
            </EscStackProvider>
          </MemoryRouter>
        </AuthProvider>,
      );
      await Promise.resolve();
    });

    const file = new File(['%PDF-1.4'], '员工手册-v2.pdf', { type: 'application/pdf' });
    await user.upload(screen.getByLabelText(copy.settings.knowledge.upload.chooseFiles), file);
    await user.click(screen.getByRole('button', { name: copy.controls.confirm }));
    await waitFor(() => expect(uploadNewVersion).toHaveBeenCalledTimes(1));

    // 提交中 Esc：useModalDialog 回调递增 operation token（旧提交失效）
    await user.keyboard('{Escape}');

    // 迟到成功：no-op（不导航 uploads、不刷新）
    await act(async () => {
      resolveUpload({ document_id: 'doc_1', document_version_id: 'dv_new', job_id: 'job_1', version: 2 });
      await Promise.resolve();
    });
    expect(onSubmitted).not.toHaveBeenCalled();
    expect(onConflictRefresh).not.toHaveBeenCalled();
  });

  it('提交中 Esc 后迟到 409：不新增 onClose/onConflictRefresh 调用', async () => {
    let rejectUpload!: (error: unknown) => void;
    const uploadNewVersion = vi.fn(() => new Promise((_resolve, reject) => (rejectUpload = reject)));
    const onSubmitted = vi.fn();
    const onConflictRefresh = vi.fn();
    const onClose = vi.fn();
    const api = {
      getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })),
      uploadNewVersion,
    } as unknown as SettingsApi;
    const user = userEvent.setup();

    await act(async () => {
      const store = await createAuthedStore();
      render(
        <AuthProvider store={store}>
          <MemoryRouter initialEntries={['/settings/knowledge']}>
            <EscStackProvider>
              <SettingsProvider
                api={api}
                authStore={store}
                theme={{ setPreference: vi.fn() } as unknown as ThemeController}
                notifications={{} as NotificationsStore}
              >
                <NewVersionDialog target={TARGET} onClose={onClose} onSubmitted={onSubmitted} onConflictRefresh={onConflictRefresh} />
              </SettingsProvider>
            </EscStackProvider>
          </MemoryRouter>
        </AuthProvider>,
      );
      await Promise.resolve();
    });

    const file = new File(['%PDF-1.4'], '员工手册-v2.pdf', { type: 'application/pdf' });
    await user.upload(screen.getByLabelText(copy.settings.knowledge.upload.chooseFiles), file);
    await user.click(screen.getByRole('button', { name: copy.controls.confirm }));
    await waitFor(() => expect(uploadNewVersion).toHaveBeenCalledTimes(1));
    await user.keyboard('{Escape}'); // 触发一次 onClose + token 失效
    const closeCallsAfterEsc = onClose.mock.calls.length;
    expect(closeCallsAfterEsc).toBeGreaterThan(0);

    // 迟到 409：不新增 onClose/onConflictRefresh
    await act(async () => {
      rejectUpload(new ApiError({ status: 409, code: 'version_conflict', message: '', details: {}, requestId: null }));
      await Promise.resolve();
    });
    expect(onClose.mock.calls.length).toBe(closeCallsAfterEsc);
    expect(onConflictRefresh).not.toHaveBeenCalled();
    expect(onSubmitted).not.toHaveBeenCalled();
  });
});
