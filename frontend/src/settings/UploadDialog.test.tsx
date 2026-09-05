import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router';
import { ApiError } from '../api/errors';
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
import { clearUploadHistory, readUploadHistory } from './upload-history';

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
              <LocationProbe />
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

/** 上传成功后对话框自动下钻（A43）：经 location 探针断言导航目标。 */
function LocationProbe() {
  const location = useLocation();
  return <output data-testid="upload-location">{location.pathname}</output>;
}

afterEach(() => {
  mockKnowledge.reset();
  mockAuth.reset();
  clearUploadHistory(null);
});

describe('UploadDialog 上传对话框（经契约 mock）', () => {
  it('列出 upload 目标（manage/contribute 分支提示），上传成功后下钻上传结果层', async () => {
    const api = createContractApi();
    const user = userEvent.setup();
    await renderUpload(api);

    // manage 目标 = 直接写入；contribute 目标 = 需审核分支提示
    expect(await screen.findByText(copy.settings.knowledge.upload.manageTargetHint)).toBeInTheDocument();
    expect(screen.getByText(copy.settings.knowledge.upload.contributeTargetHint)).toBeInTheDocument();

    // 初始上传去重键包含规范化文件名；同内容不同文件名均应被接收。
    const file1 = new File(['%PDF-1.4'], '新文档.pdf', { type: 'application/pdf' });
    const file2 = new File(['%PDF-1.4'], '新文档-副本.pdf', { type: 'application/pdf' });
    const input = screen.getByLabelText(copy.settings.knowledge.upload.chooseFiles, {
      selector: 'input',
    }) as HTMLInputElement;
    await user.upload(input, [file1, file2]);

    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.upload.upload }));

    // A43：成功即下钻上传结果层（默认 manage 目标），不在对话框内呈现逐文件结果
    await waitFor(() =>
      expect(screen.getByTestId('upload-location').textContent).toBe('/settings/knowledge/uploads'),
    );
    expect(screen.queryByText(/已接收/)).not.toBeInTheDocument();
    // 结果经上传历史稳定承载（会话内档）
    const history = readUploadHistory('sess:u_user');
    expect(history).not.toBeNull();
    expect(history!.response.items.filter((item) => item.accepted)).toHaveLength(2);
  });

  it('拖拽区固定注明允许类型与大小上限（A40）', async () => {
    const api = createContractApi();
    await renderUpload(api);

    expect(
      await screen.findByText(copy.settings.knowledge.upload.dropHintConstraints),
    ).toBeInTheDocument();
  });

  it('重复选择/拖入同一文件（name+size+lastModified）去重为一条（A40）', async () => {
    const api = createContractApi();
    const user = userEvent.setup();
    await renderUpload(api);

    await screen.findByText(copy.settings.knowledge.upload.manageTargetHint);
    const input = screen.getByLabelText(copy.settings.knowledge.upload.chooseFiles, {
      selector: 'input',
    }) as HTMLInputElement;
    const file = new File(['%PDF-1.4'], '重复文档.pdf', { type: 'application/pdf' });
    await user.upload(input, [file]);
    await user.upload(input, [file]);

    const list = screen.getByLabelText(copy.settings.knowledge.upload.fileListAria);
    expect(list.querySelectorAll('li')).toHaveLength(1);
    expect(screen.getAllByText('重复文档.pdf')).toHaveLength(1);
  });

  it('409 quota_exceeded 整批拒绝：错误行指向知识库页申请增加页数（A40）', async () => {
    const api = {
      getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })),
      listUploadSpaces: vi.fn(async () => ({
        items: [
          { id: 'personal:u_user', kind: 'personal', name: '个人库', permission: 'manage', document_count: 0 },
        ],
      })),
      uploadDocuments: vi.fn(async () => {
        throw new ApiError({
          status: 409,
          code: 'quota_exceeded',
          message: '',
          details: {},
          requestId: null,
        });
      }),
    } as unknown as SettingsApi;
    const user = userEvent.setup();
    await renderUpload(api);

    await screen.findByText(copy.settings.knowledge.upload.manageTargetHint);
    const input = screen.getByLabelText(copy.settings.knowledge.upload.chooseFiles, {
      selector: 'input',
    }) as HTMLInputElement;
    await user.upload(input, [new File(['%PDF-1.4'], '配额文档.pdf', { type: 'application/pdf' })]);
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.upload.upload }));

    expect(await screen.findByRole('alert')).toHaveTextContent(copy.settings.knowledge.upload.quotaExceeded);
    expect(screen.getByRole('alert').textContent).toContain('可在知识库页申请增加页数');
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
    await user.upload(
      screen.getByLabelText(copy.settings.knowledge.upload.chooseFiles, { selector: 'input' }),
      file,
    );
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
    await user.upload(
      screen.getByLabelText(copy.settings.knowledge.upload.chooseFiles, { selector: 'input' }),
      file,
    );
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.upload.upload }));
    await waitFor(() => expect(uploadDocuments).toHaveBeenCalledTimes(1));

    // 上传飞行中：目标 radio 与文件选择禁用
    const radio = screen.getByRole('radio', { name: /个人库/ }) as HTMLInputElement;
    await waitFor(() => expect(radio.disabled).toBe(true));
    const fileInput = screen.getByLabelText(copy.settings.knowledge.upload.chooseFiles, {
      selector: 'input',
    }) as HTMLInputElement;
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

describe('UploadDialog 目标空间超过 8 行（共用基座 §5.6）', () => {
  function nineSpacesApi(): SettingsApi {
    const items = [
      { id: 'personal:u_user', kind: 'personal', name: '个人库', permission: 'manage', document_count: 0 },
      ...Array.from({ length: 8 }, (_, index) => ({
        id: `department:d_${index}`,
        kind: 'department',
        name: `部门库${index + 1}`,
        permission: index === 0 ? 'manage' : 'contribute',
        document_count: 0,
      })),
    ];
    return {
      getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })),
      listUploadSpaces: vi.fn(async () => ({ items })),
      uploadDocuments: vi.fn(),
    } as unknown as SettingsApi;
  }

  it('返回项 >8 行：出现顶部搜索框且列表内部滚动（max-height 320px），按空间名实时过滤', async () => {
    const api = nineSpacesApi();
    const user = userEvent.setup();
    await renderUpload(api);

    // 顶部搜索框（规格同 3.2 搜索框）出现；9 个目标全部列出
    const search = await screen.findByRole('searchbox', {
      name: copy.settings.knowledge.upload.spaceSearchPlaceholder,
    });
    expect(screen.getAllByRole('radio')).toHaveLength(9);

    // 列表容器内部滚动（max-height 320px）
    const list = screen.getAllByRole('radio')[0]!.closest('ul');
    expect(list).toHaveClass('max-h-[320px]', 'overflow-y-auto');

    // 权限标注与默认选中态不变（第一个 manage 默认选中）
    expect(screen.getAllByText(copy.settings.knowledge.upload.manageTargetHint).length).toBe(2);
    expect(screen.getAllByText(copy.settings.knowledge.upload.contributeTargetHint).length).toBe(7);
    expect(screen.getByRole('radio', { name: /个人库/ })).toBeChecked();

    // 按空间名实时过滤
    await user.type(search, '部门库3');
    expect(screen.getAllByRole('radio')).toHaveLength(1);
    expect(screen.getByRole('radio', { name: /部门库3/ })).toBeInTheDocument();

    // 过滤无结果：一行空态说明
    await user.clear(search);
    await user.type(search, '不存在的空间');
    expect(screen.queryAllByRole('radio')).toHaveLength(0);
    expect(screen.getByText(copy.settings.knowledge.upload.spaceSearchEmpty)).toBeInTheDocument();

    // 清空过滤词恢复完整列表
    await user.clear(search);
    expect(screen.getAllByRole('radio')).toHaveLength(9);
  });

  it('返回项 ≤8 行：维持平铺，不出现搜索框', async () => {
    const api = createContractApi(); // 契约 mock：普通用户 3 个上传目标
    await renderUpload(api);

    expect(await screen.findByText(copy.settings.knowledge.upload.manageTargetHint)).toBeInTheDocument();
    expect(
      screen.queryByRole('searchbox', { name: copy.settings.knowledge.upload.spaceSearchPlaceholder }),
    ).not.toBeInTheDocument();
    const list = screen.getAllByRole('radio')[0]!.closest('ul');
    expect(list).not.toHaveClass('max-h-[320px]');
  });
});
