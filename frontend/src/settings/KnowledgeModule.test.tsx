import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import { ApiError } from '../api/errors';
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
import { KnowledgeModule } from './KnowledgeModule';
import { SettingsProvider } from './SettingsProvider';
import type { QuotaSnapshot } from './types';

function testUser(overrides: Partial<User> = {}): User {
  return {
    id: 'u_1',
    username: 'zhangsan',
    display_name: '张三',
    real_name: '张三',
    department: { id: 'd_finance', name: '财务部' },
    role: 'user',
    avatar_url: null,
    ...overrides,
  };
}

const SAMPLE_QUOTA: QuotaSnapshot = {
  used: 120,
  base_limit: 500,
  extra_granted: 0,
  effective_limit: 500,
  unlimited: false,
  reset_at: '2026-09-01T00:00:00+08:00',
  business_timezone: 'Asia/Shanghai',
  quota_period: '2026-08',
  business_calendar_version_id: 'calendar_1',
  pending_request: null,
};

function createSettingsApi(overrides: Partial<SettingsApi> = {}): SettingsApi {
  const personalSpace = { id: 'personal:u_1', kind: 'personal' as const, name: '个人库', permission: 'manage' as const, document_count: 3 };
  return {
    getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })),
    updatePreferences: vi.fn(async (next) => next),
    getQuota: vi.fn(async () => SAMPLE_QUOTA),
    requestQuota: vi.fn(async () => ({
      id: 'qr_1',
      version: 1,
      status: 'pending',
      requested_pages: 100,
      quota_period: '2026-08',
      created_at: '2026-08-01T00:00:00Z',
    })),
    listUploadSpaces: vi.fn(async () => ({ items: [personalSpace] })),
    listDocuments: vi.fn(async () => ({
      items: [
        {
          id: 'doc_1',
          document_version_id: 'dv_1',
          version: 1,
          name: '员工手册.pdf',
          media_kind: 'pdf',
          version_status: 'active',
          active_operation: null,
          uploaded_at: '2026-07-20T02:00:00Z',
          usage: { pages: 50, images: 40 },
        },
      ],
      total: 1,
      page: 1,
      page_size: 10,
    })),
    uploadDocuments: vi.fn(async () => ({ upload_batch_id: null, items: [] })),
    listJobs: vi.fn(async () => ({ items: [], limit: 50, max_limit: 200, has_more: false })),
    listManageSpaces: vi.fn(async () => ({ items: [] })),
    getApprovalSummary: vi.fn(async () => ({ quota_pending: 0, submission_pending: 0 })),
    ...overrides,
  } as unknown as SettingsApi;
}

async function createAuthedStore(user: User = testUser()): Promise<AuthSessionStore> {
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

async function renderKnowledge(store: AuthSessionStore, api: SettingsApi) {
  let result!: ReturnType<typeof render>;
  await act(async () => {
    result = render(
      <AuthProvider store={store}>
        <MemoryRouter initialEntries={['/']}>
          <EscStackProvider>
            <SettingsProvider
              api={api}
              authStore={store}
              theme={{ setPreference: vi.fn() } as unknown as ThemeController}
              notifications={{} as NotificationsStore}
            >
              <KnowledgeModule />
            </SettingsProvider>
          </EscStackProvider>
        </MemoryRouter>
      </AuthProvider>,
    );
    await Promise.resolve();
  });
  return result;
}

describe('KnowledgeModule 知识库首页', () => {
  it('显示配额计数器（页单位）与文档列表；pending_request 常驻行', async () => {
    const api = createSettingsApi();
    api.getQuota = vi.fn(async () => ({ ...SAMPLE_QUOTA, pending_request: { id: 'qr_1', version: 1, requested_pages: 100, quota_period: '2026-08', created_at: '2026-08-01T00:00:00Z' } }));
    const store = await createAuthedStore();
    await renderKnowledge(store, api);

    expect(await screen.findByText('120 / 500 页')).toBeInTheDocument();
    expect(screen.getByText(copy.settings.knowledge.quota.pendingRequest)).toBeInTheDocument();
    expect(await screen.findByText('员工手册.pdf')).toBeInTheDocument();
    expect(screen.getByText('50 页正文 + 40 张图')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: copy.settings.knowledge.upload.button })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: copy.settings.knowledge.submissions.entry })).toBeInTheDocument();
  });

  it('unlimited 角色：显示「不限」且无申请入口', async () => {
    const api = createSettingsApi();
    api.getQuota = vi.fn(async () => ({ ...SAMPLE_QUOTA, unlimited: true, effective_limit: 0 }));
    const store = await createAuthedStore(testUser({ role: 'ops' }));
    await renderKnowledge(store, api);

    expect(await screen.findByText(copy.settings.knowledge.quota.unlimited)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: copy.settings.knowledge.quota.requestMore })).not.toBeInTheDocument();
  });

  it('部长管理入口不请求或展示投稿待审计数', async () => {
    const api = createSettingsApi({
      listUploadSpaces: vi.fn(async () => ({
        items: [
          { id: 'personal:u_1', kind: 'personal' as const, name: '个人库', permission: 'manage' as const, document_count: 3 },
          { id: 'department:d_finance', kind: 'department' as const, name: '财务部资料库', permission: 'manage' as const, document_count: 4 },
        ],
      })),
      getApprovalSummary: vi.fn(async () => ({ quota_pending: 0, submission_pending: 7 })),
    });
    const store = await createAuthedStore(testUser({ role: 'minister' }));
    await renderKnowledge(store, api);

    const entry = (await screen.findByText(copy.settings.knowledge.manage.title)).closest('button');
    expect(entry).not.toBeNull();
    expect(entry).not.toHaveTextContent('7');
    expect(api.getApprovalSummary).not.toHaveBeenCalled();
  });

  it('配额申请：非法值禁用确认键；201 后淡入 pending 常驻行', async () => {
    const api = createSettingsApi();
    let quotaCalls = 0;
    api.getQuota = vi.fn(async () => {
      quotaCalls += 1;
      // 首次加载无 pending；201 后 loadQuota 再次请求时服务端已存在 pending_request。
      // 申请入口仅耗尽时出现（共用基座 §5.6），故 quota 恒为耗尽态
      const exhausted = { ...SAMPLE_QUOTA, used: 500 };
      return quotaCalls > 1
        ? {
            ...exhausted,
            pending_request: {
              id: 'qr_1',
              version: 1,
              requested_pages: 100,
              quota_period: '2026-08',
              created_at: '2026-08-01T00:00:00Z',
            },
          }
        : exhausted;
    });
    const requestQuota = vi.fn(async () => ({
      id: 'qr_1',
      version: 1,
      status: 'pending' as const,
      requested_pages: 100,
      quota_period: '2026-08',
      created_at: '2026-08-01T00:00:00Z',
    }));
    api.requestQuota = requestQuota;
    const store = await createAuthedStore();
    const user = userEvent.setup();
    await renderKnowledge(store, api);

    await user.click(await screen.findByRole('button', { name: copy.settings.knowledge.quota.requestMore }));
    const dialog = screen.getByRole('dialog', { name: copy.settings.knowledge.quota.requestDialogTitle });
    expect(dialog).toBeInTheDocument();

    // 空输入：确认键禁用
    const confirm = screen.getByRole('button', { name: copy.controls.confirm });
    expect(confirm).toBeDisabled();

    const input = screen.getByLabelText(copy.settings.knowledge.quota.requestedPagesLabel);
    await user.type(input, '100');
    expect(confirm).toBeEnabled();

    await user.click(confirm);
    await waitFor(() => expect(requestQuota).toHaveBeenCalledWith(100, expect.stringMatching(/^idem_/)));
    await waitFor(() =>
      expect(screen.getByText(copy.settings.knowledge.quota.pendingRequest)).toBeInTheDocument(),
    );
  });

  it('配额申请 409 pending_request_exists 就地提示', async () => {
    const api = createSettingsApi();
    api.getQuota = vi.fn(async () => ({ ...SAMPLE_QUOTA, used: 500 }));
    api.requestQuota = vi.fn(async () => {
      throw new ApiError({ status: 409, code: 'pending_request_exists', message: '', details: {}, requestId: null });
    });
    const store = await createAuthedStore();
    const user = userEvent.setup();
    await renderKnowledge(store, api);

    await user.click(await screen.findByRole('button', { name: copy.settings.knowledge.quota.requestMore }));
    const input = screen.getByLabelText(copy.settings.knowledge.quota.requestedPagesLabel);
    await user.type(input, '100');
    await user.click(screen.getByRole('button', { name: copy.controls.confirm }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      copy.settings.knowledge.quota.pendingRequestExists,
    );
  });

  it('文档行操作：仅 manage 渲染；删除 202 后立即移除', async () => {
    const api = createSettingsApi();
    api.deleteDocument = vi.fn(async () => {});
    const store = await createAuthedStore();
    const user = userEvent.setup();
    await renderKnowledge(store, api);

    const rowMenu = await screen.findByRole('button', { name: copy.settings.knowledge.documents.rowMenuAria('员工手册.pdf') });
    await user.click(rowMenu);
    await user.click(await screen.findByRole('menuitem', { name: copy.settings.knowledge.documents.delete }));

    // 删除二次确认：说明固定两点（共用基座 §5.6）
    const dialog = await screen.findByRole('dialog', { name: copy.settings.knowledge.documents.deleteConfirmTitle });
    expect(within(dialog).getByText(copy.settings.knowledge.documents.deleteConfirmDescription)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.documents.delete }));

    await waitFor(() => expect(api.deleteDocument).toHaveBeenCalledWith('doc_1', 1, expect.stringMatching(/^idem_/)));
    await waitFor(() => expect(screen.queryByText('员工手册.pdf')).not.toBeInTheDocument());
  });
});

describe('KnowledgeModule mutation 代际与 single-flight（review A1/A5）', () => {
  it('删除进行中 Esc 取消：旧 mutation 失效，迟到成功不写入视图（列表/页码不变）', async () => {
    let resolveDelete!: () => void;
    const deleteDocument = vi.fn(() => new Promise<void>((resolve) => (resolveDelete = resolve)));
    const listDocuments = vi.fn(async () => ({
      items: [
        {
          id: 'doc_1',
          document_version_id: 'dv_1',
          version: 1,
          name: '员工手册.pdf',
          media_kind: 'pdf',
          version_status: 'active',
          active_operation: null,
          uploaded_at: '2026-07-20T02:00:00Z',
          usage: { pages: 50, images: 40 },
        },
      ],
      total: 1,
      page: 1,
      page_size: 10,
    }));
    const api = createSettingsApi({ deleteDocument, listDocuments });
    const store = await createAuthedStore();
    const user = userEvent.setup();
    await renderKnowledge(store, api);

    await user.click(await screen.findByRole('button', { name: copy.settings.knowledge.documents.rowMenuAria('员工手册.pdf') }));
    await user.click(await screen.findByRole('menuitem', { name: copy.settings.knowledge.documents.delete }));
    const dialog = await screen.findByRole('dialog', { name: copy.settings.knowledge.documents.deleteConfirmTitle });
    const confirmButton = within(dialog).getByRole('button', { name: copy.settings.knowledge.documents.delete }) as HTMLButtonElement;
    await user.click(confirmButton);
    await waitFor(() => expect(deleteDocument).toHaveBeenCalledTimes(1));

    // 确认期间 single-flight：confirm disabled + aria-busy（双击只发一次）
    await waitFor(() => expect(confirmButton.disabled).toBe(true));
    expect(deleteDocument).toHaveBeenCalledTimes(1);

    // Esc 取消：关闭确认框、清 pending 意图、旧 mutation 失效
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    // 旧删除迟到成功：不得写入视图（行仍在、total 未变）
    await act(async () => {
      resolveDelete();
      await Promise.resolve();
    });
    expect(screen.getByText('员工手册.pdf')).toBeInTheDocument();
    expect(screen.queryByText(copy.controls.pageIndicator(1, 1))).toBeNull(); // total=1 → 无页码器
    expect(listDocuments.mock.calls.length).toBe(1); // 未触发旧 query 刷新
  });

  it('删除 409 冲突：清 pending 意图并刷新当前视图（不用旧 query 覆盖）', async () => {
    const deleteDocument = vi.fn(async () => {
      throw new ApiError({ status: 409, code: 'version_conflict', message: '', details: {}, requestId: null });
    });
    const listDocuments = vi.fn(async () => ({
      items: [
        {
          id: 'doc_1',
          document_version_id: 'dv_1',
          version: 2, // 刷新后最新 version
          name: '员工手册.pdf',
          media_kind: 'pdf',
          version_status: 'active',
          active_operation: null,
          uploaded_at: '2026-07-20T02:00:00Z',
          usage: { pages: 50, images: 40 },
        },
      ],
      total: 1,
      page: 1,
      page_size: 10,
    }));
    const api = createSettingsApi({ deleteDocument, listDocuments });
    const store = await createAuthedStore();
    const user = userEvent.setup();
    await renderKnowledge(store, api);

    await user.click(await screen.findByRole('button', { name: copy.settings.knowledge.documents.rowMenuAria('员工手册.pdf') }));
    await user.click(await screen.findByRole('menuitem', { name: copy.settings.knowledge.documents.delete }));
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.documents.delete }));

    // 冲突：对话框关闭、列表按当前视图刷新（listDocuments 再次被调用）
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(listDocuments.mock.calls.length).toBeGreaterThan(1));
    expect(screen.getByText(copy.settings.knowledge.submissions.versionConflict)).toBeInTheDocument();
  });
});

describe('KnowledgeModule confirming 释放（review A1）', () => {
  it('请求中 Esc 关闭后再打开另一条确认：新确认键不被旧 confirming 锁死', async () => {
    let resolveDelete!: () => void;
    const deleteDocument = vi.fn(() => new Promise<void>((resolve) => (resolveDelete = resolve)));
    const listDocuments = vi.fn(async () => ({
      items: [
        {
          id: 'doc_1',
          document_version_id: 'dv_1',
          version: 1,
          name: '员工手册.pdf',
          media_kind: 'pdf',
          version_status: 'active',
          active_operation: null,
          uploaded_at: '2026-07-20T02:00:00Z',
          usage: { pages: 50, images: 40 },
        },
      ],
      total: 1,
      page: 1,
      page_size: 10,
    }));
    const api = createSettingsApi({ deleteDocument, listDocuments });
    const store = await createAuthedStore();
    const user = userEvent.setup();
    await renderKnowledge(store, api);

    // 打开删除确认并确认（请求挂起 → confirming=true）
    await user.click(await screen.findByRole('button', { name: copy.settings.knowledge.documents.rowMenuAria('员工手册.pdf') }));
    await user.click(await screen.findByRole('menuitem', { name: copy.settings.knowledge.documents.delete }));
    let dialog = await screen.findByRole('dialog', { name: copy.settings.knowledge.documents.deleteConfirmTitle });
    let confirmButton = within(dialog).getByRole('button', { name: copy.settings.knowledge.documents.delete }) as HTMLButtonElement;
    await user.click(confirmButton);
    await waitFor(() => expect(confirmButton.disabled).toBe(true));

    // Esc 关闭：立即释放 confirming（对话框关闭）
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    // 再打开另一条确认：确认键不得被旧 confirming 锁死（enabled）
    await user.click(await screen.findByRole('button', { name: copy.settings.knowledge.documents.rowMenuAria('员工手册.pdf') }));
    await user.click(await screen.findByRole('menuitem', { name: copy.settings.knowledge.documents.delete }));
    dialog = await screen.findByRole('dialog', { name: copy.settings.knowledge.documents.deleteConfirmTitle });
    confirmButton = within(dialog).getByRole('button', { name: copy.settings.knowledge.documents.delete }) as HTMLButtonElement;
    await waitFor(() => expect(confirmButton.disabled).toBe(false));

    // 旧请求迟到成功：不得关闭新确认框、不得移除行（旧 mutation 已失效）
    await act(async () => {
      resolveDelete();
      await Promise.resolve();
    });
    expect(screen.getByRole('dialog', { name: copy.settings.knowledge.documents.deleteConfirmTitle })).toBeInTheDocument();
    expect(screen.getByText('员工手册.pdf')).toBeInTheDocument();
  });
});

describe('KnowledgeModule 409 刷新挂起期间切视图（review Medium 3）', () => {
  it('delete 409 后刷新挂起时切换搜索：旧 finally 不锁 confirming、错误不写新视图', async () => {
    let resolveListRefresh!: () => void;
    let refreshRequested = false;
    const deleteDocument = vi.fn(async () => {
      refreshRequested = true; // 409 后触发刷新：后续 listDocuments 挂起
      throw new ApiError({ status: 409, code: 'version_conflict', message: '', details: {}, requestId: null });
    });
    const docRow = (version: number) => ({
      id: 'doc_1',
      document_version_id: 'dv_1',
      version,
      name: '员工手册.pdf',
      media_kind: 'pdf',
      version_status: 'active',
      active_operation: null,
      uploaded_at: '2026-07-20T02:00:00Z',
      usage: { pages: 50, images: 40 },
    });
    const listDocuments = vi.fn((): Promise<{ items: ReturnType<typeof docRow>[]; total: number; page: number; page_size: number }> => {
      if (!refreshRequested) {
        return Promise.resolve({ items: [docRow(1)], total: 1, page: 1, page_size: 10 });
      }
      return new Promise((resolve) => {
        resolveListRefresh = () => resolve({ items: [docRow(2)], total: 1, page: 1, page_size: 10 });
      });
    });
    const api = createSettingsApi({ deleteDocument, listDocuments });
    const store = await createAuthedStore();
    const user = userEvent.setup();
    await renderKnowledge(store, api);

    // 打开删除确认并确认 → 409 → 进入 await 刷新（挂起）
    await user.click(await screen.findByRole('button', { name: copy.settings.knowledge.documents.rowMenuAria('员工手册.pdf') }));
    await user.click(await screen.findByRole('menuitem', { name: copy.settings.knowledge.documents.delete }));
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.documents.delete }));
    await waitFor(() => expect(deleteDocument).toHaveBeenCalledTimes(1));

    // 刷新挂起期间切换搜索（作废旧操作并释放 confirming；搜索本身也会请求列表）
    const searchInput = screen.getByLabelText(copy.settings.knowledge.documents.searchAria);
    await user.type(searchInput, '报销');
    await user.keyboard('{Enter}');
    await waitFor(() => expect(listDocuments.mock.calls.length).toBeGreaterThan(1));

    // 旧刷新 resolve：迟到的 error 写入必须被 epoch 检查拦截（不覆盖新视图）
    await act(async () => {
      resolveListRefresh();
      await Promise.resolve();
    });
    // 新视图无删除冲突错误（旧 409 的 error 写入被拦截）
    expect(screen.queryByText(copy.settings.knowledge.submissions.versionConflict)).not.toBeInTheDocument();
  });
});

describe('KnowledgeModule 共享幂等 scope 不被旧 mutation 清理（review Medium）', () => {
  it('delete A 关闭后启动 B，A 迟到 409 不清 B 的 Idempotency-Key（B 重试复用同键）', async () => {
    const docRow = (id: string, name: string, version: number) => ({
      id,
      document_version_id: `dv_${id}_1`,
      version,
      name,
      media_kind: 'pdf',
      version_status: 'active' as const,
      active_operation: null,
      uploaded_at: '2026-07-20T02:00:00Z',
      usage: { pages: 1, images: 0 },
    });
    const listDocuments = vi.fn(async () => ({
      items: [docRow('doc_A', '员工手册.pdf', 1), docRow('doc_B', '报销制度.docx', 1)],
      total: 2,
      page: 1,
      page_size: 10,
    }));

    const deferred = () => {
      let resolve!: (value: unknown) => void;
      let reject!: (reason: unknown) => void;
      const promise = new Promise((res, rej) => {
        resolve = res;
        reject = rej;
      });
      return { promise, resolve, reject };
    };
    const callA = deferred();
    const callB1 = deferred();
    const callB2 = deferred();
    const deleteDocument = vi
      .fn()
      .mockReturnValueOnce(callA.promise)
      .mockReturnValueOnce(callB1.promise)
      .mockReturnValueOnce(callB2.promise);

    const api = createSettingsApi({ deleteDocument, listDocuments });
    const store = await createAuthedStore();
    const user = userEvent.setup();
    await renderKnowledge(store, api);

    // 打开 A 的删除确认并确认（挂起）→ 捕获 keyA
    await user.click(await screen.findByRole('button', { name: copy.settings.knowledge.documents.rowMenuAria('员工手册.pdf') }));
    await user.click(await screen.findByRole('menuitem', { name: copy.settings.knowledge.documents.delete }));
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.documents.delete }));
    await waitFor(() => expect(deleteDocument).toHaveBeenCalledTimes(1));
    const keyA = deleteDocument.mock.calls[0]?.[2] as string;

    // Esc 关闭 A 的确认（epoch 递增，A 失效）→ 打开并确认 B（挂起）→ 捕获 keyB
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.documents.rowMenuAria('报销制度.docx') }));
    await user.click(await screen.findByRole('menuitem', { name: copy.settings.knowledge.documents.delete }));
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.documents.delete }));
    await waitFor(() => expect(deleteDocument).toHaveBeenCalledTimes(2));
    const keyB = deleteDocument.mock.calls[1]?.[2] as string;
    expect(keyB).not.toBe(keyA);

    // A 迟到 409：旧 mutation 不得清共享 scope（B 的 key 保持），也不得写错误/关 B 确认框
    await act(async () => {
      callA.reject(new ApiError({ status: 409, code: 'version_conflict', message: '', details: {}, requestId: null }));
      await Promise.resolve();
    });
    expect(screen.queryByText(copy.settings.knowledge.submissions.versionConflict)).not.toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: copy.settings.knowledge.documents.deleteConfirmTitle })).toBeInTheDocument();

    // B 关闭确认 → 重开 → 再确认：Idempotency-Key 必须与首次 B 相同（未被 A 的 409 清掉）
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.documents.rowMenuAria('报销制度.docx') }));
    await user.click(await screen.findByRole('menuitem', { name: copy.settings.knowledge.documents.delete }));
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.documents.delete }));
    await waitFor(() => expect(deleteDocument).toHaveBeenCalledTimes(3));
    const keyBRetry = deleteDocument.mock.calls[2]?.[2] as string;
    expect(keyBRetry).toBe(keyB);

    // 收尾：B 成功完成
    await act(async () => {
      callB1.reject(new ApiError({ status: null, code: 'timeout', message: '', details: {}, requestId: null }));
      callB2.resolve(undefined);
      await Promise.resolve();
    });
  });
});
