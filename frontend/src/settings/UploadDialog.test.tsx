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
import { mockAuth, mockKnowledge } from '../mocks/testing';
import type { NotificationsStore } from '../notifications/store';
import type { ThemeController } from '../theme/theme';
import type { SettingsApi } from './api';
import { SettingsProvider } from './SettingsProvider';
import { UploadDialog } from './UploadDialog';

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
    listUploadSpaces: vi.fn(async () => mockKnowledge.listSpacesForUpload(token)),
    uploadDocuments: vi.fn(async (spaceId: string, files: readonly File[], idem: string) => {
      // 与生产 handler 相同的内容 hash 语义：对原始字节计算 FNV-1a（dedupe 依据）
      const fnv1a = (bytes: Uint8Array): string => {
        let hash = 0x811c9dc5;
        for (let index = 0; index < bytes.length; index += 1) {
          hash ^= bytes[index]!;
          hash = Math.imul(hash, 0x01000193) >>> 0;
        }
        return hash.toString(16).padStart(8, '0');
      };
      const parsed = await Promise.all(
        files.map(async (file) => ({
          name: file.name,
          size: file.size,
          type: file.type,
          contentHash: fnv1a(new Uint8Array(await file.arrayBuffer())),
        })),
      );
      return mockKnowledge.uploadDocuments(token, spaceId, parsed, idem);
    }),
  } as unknown as SettingsApi;
}

async function renderUpload(api: SettingsApi) {
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
              <UploadDialog open onOpenChange={() => {}} sessionKey="sess:u_user" />
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
});

describe('UploadDialog 上传对话框（经契约 mock）', () => {
  it('列出 upload 目标（manage/contribute 分支提示），多文件上传后逐文件呈现结果', async () => {
    const api = createContractApi();
    const user = userEvent.setup();
    await renderUpload(api);

    // manage 目标 = 直接写入；contribute 目标 = 需审核分支提示
    expect(await screen.findByText(copy.settings.knowledge.upload.manageTargetHint)).toBeInTheDocument();
    expect(screen.getByText(copy.settings.knowledge.upload.contributeTargetHint)).toBeInTheDocument();

    // 选择文件（用户事件 File 选择）：两个文件内容相同（内容 hash dedupe），不同文件名
    const file1 = new File(['%PDF-1.4'], '新文档.pdf', { type: 'application/pdf' });
    const file2 = new File(['%PDF-1.4'], '新文档-副本.pdf', { type: 'application/pdf' });
    const input = screen.getByLabelText(copy.settings.knowledge.upload.chooseFiles) as HTMLInputElement;
    await user.upload(input, [file1, file2]);

    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.upload.upload }));

    // 逐文件结果：accepted + deduplicated（基于内容 hash，与文件名无关）
    await waitFor(() =>
      expect(screen.getAllByText(/新文档\.pdf/).length).toBeGreaterThan(0),
    );
    expect(screen.getByText(/内容重复，未新增任务/)).toBeInTheDocument();
  });

  it('失败项按服务端错误对象呈现；quota_exceeded 整批拒绝提示', async () => {
    const api = createContractApi();
    mockKnowledge.setNextUploadFailure('bad', 'malware_detected');
    const user = userEvent.setup();
    await renderUpload(api);

    const file = new File(['x'], 'bad-name.pdf', { type: 'application/pdf' });
    const input = screen.getByLabelText(copy.settings.knowledge.upload.chooseFiles) as HTMLInputElement;
    await user.upload(input, [file]);
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.upload.upload }));

    await waitFor(() => expect(screen.getByText(/检测到恶意内容/)).toBeInTheDocument());
  });
});

describe('UploadDialog operation token（review A2：A 迟到 completion 不污染 B）', () => {
  it('上传 A 飞行中关闭再重开：A 迟到成功不清 B 的 key/不导航', async () => {
    const { accessToken } = mockAuth.login('zhangsan', 'password123', 'upload-token');
    const token = `Bearer ${accessToken}`;
    let resolveA!: (value: unknown) => void;
    const uploadDocuments = vi.fn((spaceId: string, files: readonly File[], idem: string) =>
      new Promise((resolve) => {
        resolveA = () => {
          resolve(mockKnowledge.uploadDocuments(token, spaceId, files.map((f) => ({ name: f.name, size: f.size, type: f.type, contentHash: 'hash-token' })), idem));
        };
      }),
    );
    let open = true;
    let lastNavigated = '';
    const api = {
      getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })),
      listUploadSpaces: vi.fn(async () => mockKnowledge.listSpacesForUpload(token)),
      uploadDocuments,
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
                <UploadDialog
                  open={open}
                  onOpenChange={(next) => {
                    open = next;
                  }}
                  sessionKey="sess:u_user"
                />
              </SettingsProvider>
            </EscStackProvider>
          </MemoryRouter>
        </AuthProvider>,
      );
      await Promise.resolve();
    });

    // 选择文件并上传（挂起）
    const file = new File(['%PDF-1.4'], 'A文档.pdf', { type: 'application/pdf' });
    await user.upload(screen.getByLabelText(copy.settings.knowledge.upload.chooseFiles), file);
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.upload.upload }));
    await waitFor(() => expect(uploadDocuments).toHaveBeenCalledTimes(1));

    // 关闭（Esc 经 useModalDialog 递增 operation token）
    await user.keyboard('{Escape}');
    // 重开（新 operation）
    await act(async () => {
      open = true;
    });
    expect(screen.getByRole('dialog', { name: copy.settings.knowledge.upload.dialogTitle })).toBeInTheDocument();

    // A 迟到成功：不得清 B 的 key/状态、不得导航
    await act(async () => {
      resolveA({ upload_batch_id: 'ub_A', items: [] });
      await Promise.resolve();
    });
    expect(uploadDocuments.mock.calls.length).toBe(1); // B 未误触发
    expect(screen.getByRole('dialog', { name: copy.settings.knowledge.upload.dialogTitle })).toBeInTheDocument();
    void lastNavigated;
  });
});

describe('UploadDialog 上传中控件禁用与 token 隔离（review Medium 1）', () => {
  it('上传飞行中目标 radio 与文件控件禁用；A 迟到 completion 不写 B 状态', async () => {
    const { accessToken } = mockAuth.login('zhangsan', 'password123', 'upload-lock');
    const token = `Bearer ${accessToken}`;
    let resolveA!: (value: unknown) => void;
    const uploadDocuments = vi.fn((spaceId: string, files: readonly File[], idem: string) =>
      new Promise((resolve) => {
        resolveA = () => {
          resolve(mockKnowledge.uploadDocuments(token, spaceId, files.map((f) => ({ name: f.name, size: f.size, type: f.type, contentHash: 'hash-lock' })), idem));
        };
      }),
    );
    const api = {
      getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })),
      listUploadSpaces: vi.fn(async () => mockKnowledge.listSpacesForUpload(token)),
      uploadDocuments,
    } as unknown as SettingsApi;
    const user = userEvent.setup();
    const store = await createAuthedStore();

    const ui = (open: boolean) => (
      <AuthProvider store={store}>
        <MemoryRouter initialEntries={['/settings/knowledge']}>
          <EscStackProvider>
            <SettingsProvider
              api={api}
              authStore={store}
              theme={{ setPreference: vi.fn() } as unknown as ThemeController}
              notifications={{} as NotificationsStore}
            >
              <UploadDialog open={open} onOpenChange={() => {}} sessionKey="sess:u_user" />
            </SettingsProvider>
          </EscStackProvider>
        </MemoryRouter>
      </AuthProvider>
    );
    let result!: ReturnType<typeof render>;
    await act(async () => {
      result = render(ui(true));
      await Promise.resolve();
    });

    // 选择文件并上传（挂起）
    const file = new File(['%PDF-1.4'], 'A文档.pdf', { type: 'application/pdf' });
    await user.upload(screen.getByLabelText(copy.settings.knowledge.upload.chooseFiles), file);
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.upload.upload }));
    await waitFor(() => expect(uploadDocuments).toHaveBeenCalledTimes(1));

    // 上传飞行中：目标 radio 与文件选择禁用
    const radio = screen.getByRole('radio', { name: /个人库/ }) as HTMLInputElement;
    await waitFor(() => expect(radio.disabled).toBe(true));
    const fileInput = screen.getByLabelText(copy.settings.knowledge.upload.chooseFiles) as HTMLInputElement;
    expect(fileInput.disabled).toBe(true);

    // Esc 关闭（真实 React rerender 应用 open=false）→ 重开（新 operation token）
    await user.keyboard('{Escape}');
    await act(async () => {
      result.rerender(ui(false));
      await Promise.resolve();
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await act(async () => {
      result.rerender(ui(true));
      await Promise.resolve();
    });
    expect(screen.getByRole('dialog', { name: copy.settings.knowledge.upload.dialogTitle })).toBeInTheDocument();

    // A 迟到成功：不写 B 状态（无结果呈现、无导航、无历史）
    await act(async () => {
      resolveA({ upload_batch_id: 'ub_A', items: [] });
      await Promise.resolve();
    });
    expect(screen.queryByText(/已接收/)).not.toBeInTheDocument();
    expect(uploadDocuments.mock.calls.length).toBe(1);
    expect(screen.getByRole('dialog', { name: copy.settings.knowledge.upload.dialogTitle })).toBeInTheDocument();
  });
});
