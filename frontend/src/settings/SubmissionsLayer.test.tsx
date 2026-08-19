import { act, render, screen, waitFor, within } from '@testing-library/react';
import { ApiError } from '../api/errors';
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
import { SubmissionsLayer } from './SubmissionsLayer';

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

/** 经契约 mock 的 SettingsApi：直接代理到 MockKnowledgeController（与真实 handler 同源）。 */
function createContractApi(): SettingsApi {
  const { accessToken } = mockAuth.login('zhangsan', 'password123', 'vitest');
  const token = `Bearer ${accessToken}`;
  return {
    getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })),
    listSubmissions: vi.fn(async (status) => mockKnowledge.listSubmissions(token, status as never)),
    withdrawSubmission: vi.fn(async (submissionId, version, idem) =>
      mockKnowledge.withdrawSubmission(token, submissionId, version, idem),
    ),
    deleteSubmission: vi.fn(async (submissionId, version) => {
      mockKnowledge.deleteSubmission(token, submissionId, version);
    }),
    getSubmissionContent: vi.fn(async (submissionId) => {
      const content = mockKnowledge.getSubmissionContent(token, submissionId);
      return new Blob([content.bytes as BlobPart], { type: content.type });
    }),
  } as unknown as SettingsApi;
}

async function renderLayer(api: SettingsApi) {
  const store = await createAuthedStore();
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
              <SubmissionsLayer />
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

describe('SubmissionsLayer 我的投稿层（经契约 mock 真实运行）', () => {
  it('筛选切换重新请求；撤回固定两点确认后行原地保留转已撤回', async () => {
    // 新增一个公开库投稿（种子 + 新增均可见）
    const api = createContractApi();
    const { accessToken } = mockAuth.login('zhangsan', 'password123', 'direct');
    const upload = await mockKnowledge.uploadDocuments(
      `Bearer ${accessToken}`,
      'public',
      [{ name: '待撤回稿件.md', size: 5, type: 'text/markdown' }],
      'idem-layer-1',
    );
    expect(upload.items[0]?.status).toBe('pending');
    const user = userEvent.setup();

    await renderLayer(api);

    // 全部档可见种子 + 新增投稿
    expect(await screen.findByText('待撤回稿件.md')).toBeInTheDocument();
    expect(screen.getAllByText(copy.settings.knowledge.submissions.statusTag.pending).length).toBeGreaterThan(0);

    // 撤回：固定两点说明二次确认；200 后行保留转「已撤回」
    await user.click(screen.getAllByRole('button', { name: copy.settings.knowledge.submissions.withdraw })[0]!);
    expect(screen.getByText(copy.settings.knowledge.submissions.withdrawConfirmDescription)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.submissions.withdraw }));

    await waitFor(() =>
      expect(
        screen.getAllByText(copy.settings.knowledge.submissions.statusTag.withdrawn).length,
      ).toBeGreaterThan(0),
    );
    expect(screen.getByText('待撤回稿件.md')).toBeInTheDocument();
  });

  it('已驳回行可删除：204 后行收拢移除', async () => {
    const api = createContractApi();
    const user = userEvent.setup();

    // 部长驳回种子投稿中的一条（固定原因）
    const ministerToken = `Bearer ${mockAuth.login('minister-li', 'password123', 'minister-device').accessToken}`;
    const approvals = mockKnowledge.listApprovals(ministerToken);
    const target = approvals.items[0];
    mockKnowledge.rejectSubmission(ministerToken, target.submission_id, target.version, '格式不符合要求');

    await renderLayer(api);

    // 五态种子引入多个可删除行：按目标文件名定位其所在行的「删除」
    const targetRow = (await screen.findByText(target.file_name)).closest('li') as HTMLElement;
    await user.click(
      within(targetRow).getByRole('button', { name: copy.settings.knowledge.submissions.delete }),
    );
    expect(screen.getByText(copy.settings.knowledge.submissions.deleteConfirmDescription(target.file_name))).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.submissions.delete }));

    await waitFor(() => expect(screen.queryByText(target.file_name)).not.toBeInTheDocument());
  });
});

describe('SubmissionsLayer 撤回行保留（《用户端设计.md》§5.2 / D1）', () => {
  it('pending 筛选下撤回成功：行原地保留转「已撤回」，操作换为「查看内容」+「删除」', async () => {
    const api = createContractApi();
    const user = userEvent.setup();
    // 契约 mock：创建一个 pending 投稿
    const { accessToken } = mockAuth.login('zhangsan', 'password123', 'fence-device');
    const upload = await mockKnowledge.uploadDocuments(
      `Bearer ${accessToken}`,
      'public',
      [{ name: '待撤回-筛选.md', size: 5, type: 'text/markdown', contentHash: 'hash-fence-1' }],
      'idem-fence-1',
    );
    const item = upload.items[0];
    const submissionId = item && 'submission_id' in item ? item.submission_id : '';

    await renderLayer(api);
    expect(await screen.findByText('待撤回-筛选.md')).toBeInTheDocument();

    // 切到「待审核」筛选
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.submissions.filterAria(copy.settings.knowledge.submissions.filters.pending) }));
    expect(await screen.findByText('待撤回-筛选.md')).toBeInTheDocument();

    // 撤回
    await user.click(screen.getAllByRole('button', { name: copy.settings.knowledge.submissions.withdraw })[0]);
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.submissions.withdraw }));

    // 行原地保留：状态 tag 转「已撤回」，行操作换为「查看内容」+「删除」，不再出现「撤回」
    const row = (await screen.findByText('待撤回-筛选.md')).closest('li') as HTMLElement;
    await waitFor(() =>
      expect(within(row).getByText(copy.settings.knowledge.submissions.statusTag.withdrawn)).toBeInTheDocument(),
    );
    // §5.2 交叉淡变：就地状态迁移后 tag 挂一次性淡入类（--duration-fast）
    expect(
      within(row).getByText(copy.settings.knowledge.submissions.statusTag.withdrawn).className,
    ).toContain('ui-fade-enter-fast');
    expect(within(row).getByRole('button', { name: copy.settings.knowledge.submissions.viewContent })).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: copy.settings.knowledge.submissions.delete })).toBeInTheDocument();
    expect(within(row).queryByRole('button', { name: copy.settings.knowledge.submissions.withdraw })).not.toBeInTheDocument();
    // 服务端确实转 withdrawn（数据验证而非 mock 自证）
    const all = mockKnowledge.listSubmissions(`Bearer ${accessToken}`, 'withdrawn');
    expect(all.items.some((submission) => submission.submission_id === submissionId)).toBe(true);
  });

  it('撤回后下一次按筛选重新请求：按服务端结果呈现（pending 筛选不再含已撤回行）', async () => {
    const api = createContractApi();
    const user = userEvent.setup();
    const { accessToken } = mockAuth.login('zhangsan', 'password123', 'refetch-device');
    await mockKnowledge.uploadDocuments(
      `Bearer ${accessToken}`,
      'public',
      [{ name: '待撤回-重拉.md', size: 5, type: 'text/markdown', contentHash: 'hash-refetch-1' }],
      'idem-refetch-1',
    );

    await renderLayer(api);
    expect(await screen.findByText('待撤回-重拉.md')).toBeInTheDocument();

    // 切到「待审核」筛选并撤回：行原地保留转「已撤回」
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.submissions.filterAria(copy.settings.knowledge.submissions.filters.pending) }));
    expect(await screen.findByText('待撤回-重拉.md')).toBeInTheDocument();
    await user.click(screen.getAllByRole('button', { name: copy.settings.knowledge.submissions.withdraw })[0]);
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.submissions.withdraw }));
    const row = (await screen.findByText('待撤回-重拉.md')).closest('li') as HTMLElement;
    await waitFor(() =>
      expect(within(row).getByText(copy.settings.knowledge.submissions.statusTag.withdrawn)).toBeInTheDocument(),
    );

    // 重新按「待审核」筛选请求后：按服务端结果呈现（该行不再出现）
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.submissions.filterAria(copy.settings.knowledge.submissions.filters.all) }));
    expect(await screen.findByText('待撤回-重拉.md')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.submissions.filterAria(copy.settings.knowledge.submissions.filters.pending) }));
    await waitFor(() => expect(screen.queryByText('待撤回-重拉.md')).not.toBeInTheDocument());
  });
});

describe('SubmissionsLayer mutation filter generation（review A4）', () => {
  it('撤回进行中切换 filter：迟到成功不写入旧视图（新 filter 列表不受污染）', async () => {
    const api = createContractApi();
    const user = userEvent.setup();
    const { accessToken } = mockAuth.login('zhangsan', 'password123', 'fgen');
    const token = `Bearer ${accessToken}`;
    // 创建投稿
    const upload = await mockKnowledge.uploadDocuments(token, 'public', [
      { name: '代际撤回.md', size: 5, type: 'text/markdown', contentHash: 'hash-fgen-1' },
    ], 'idem-fgen-1');

    // 可控延迟 withdraw
    let resolveWithdraw!: () => void;
    const withdrawSubmission = vi.fn((submissionId: string, version: number, idem: string) =>
      new Promise<void>((resolve) => {
        resolveWithdraw = () => {
          mockKnowledge.withdrawSubmission(token, submissionId, version, idem);
          resolve();
        };
      }),
    );
    const api2 = {
      getPreferences: api.getPreferences,
      listSubmissions: api.listSubmissions,
      withdrawSubmission,
      deleteSubmission: api.deleteSubmission,
      getSubmissionContent: api.getSubmissionContent,
    } as unknown as SettingsApi;
    await renderLayer(api2);

    // 切到 pending 筛选
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.submissions.filterAria(copy.settings.knowledge.submissions.filters.pending) }));
    expect(await screen.findByText('代际撤回.md')).toBeInTheDocument();

    // 发起撤回（请求挂起）
    await user.click(screen.getAllByRole('button', { name: copy.settings.knowledge.submissions.withdraw })[0]);
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.submissions.withdraw }));
    await waitFor(() => expect(withdrawSubmission).toHaveBeenCalledTimes(1));

    // 关闭确认框（Esc 同样使旧 mutation 失效），再切换 filter
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.submissions.filterAria(copy.settings.knowledge.submissions.filters.all) }));
    expect(await screen.findByText('代际撤回.md')).toBeInTheDocument();

    // 旧撤回迟到成功：不写入（行仍保留 pending 状态显示于 all 视图由新加载决定）
    await act(async () => {
      resolveWithdraw();
      await Promise.resolve();
    });
    // 服务端确已 withdrawn（数据验证）
    const withdrawn = mockKnowledge.listSubmissions(token, 'withdrawn');
    expect(withdrawn.items.some((item) => item.file_name === '代际撤回.md')).toBe(true);
    void upload;
  });
});

describe('SubmissionsLayer 409 epoch fence（review A2）', () => {
  it('撤回 409 迟到（已切换 filter）：不刷新旧 filter、不覆盖当前视图错误', async () => {
    const user = userEvent.setup();
    const { accessToken } = mockAuth.login('zhangsan', 'password123', '409fence');
    const token = `Bearer ${accessToken}`;
    const upload = await mockKnowledge.uploadDocuments(token, 'public', [
      { name: '409fence.md', size: 5, type: 'text/markdown', contentHash: 'hash-409fence' },
    ], 'idem-409fence');

    // 可控 409：先挂起，切换 filter 后再拒绝
    let rejectWithdraw!: (error: unknown) => void;
    const withdrawSubmission = vi.fn(() => new Promise<void>((_resolve, reject) => {
      rejectWithdraw = reject;
    }));
    const listSubmissions = vi.fn(async (status) => mockKnowledge.listSubmissions(token, status as never));
    const api2 = {
      getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })),
      listSubmissions,
      withdrawSubmission,
      deleteSubmission: vi.fn(async () => {}),
      getSubmissionContent: vi.fn(async () => new Blob(['x'], { type: 'text/plain' })),
    } as unknown as SettingsApi;
    await renderLayer(api2);

    // pending 筛选
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.submissions.filterAria(copy.settings.knowledge.submissions.filters.pending) }));
    expect(await screen.findByText('409fence.md')).toBeInTheDocument();

    // 发起撤回（挂起）
    await user.click(screen.getAllByRole('button', { name: copy.settings.knowledge.submissions.withdraw })[0]);
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.submissions.withdraw }));
    await waitFor(() => expect(withdrawSubmission).toHaveBeenCalledTimes(1));
    await user.keyboard('{Escape}'); // 关闭确认框（epoch 递增）

    // 切换到全部（触发一次新 filter 加载）
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.submissions.filterAria(copy.settings.knowledge.submissions.filters.all) }));
    expect(await screen.findByText('409fence.md')).toBeInTheDocument();
    await waitFor(() => expect(listSubmissions).toHaveBeenLastCalledWith('all'));
    const callsBeforeReject = listSubmissions.mock.calls.length;

    // 旧 409 迟到：不得 loadSubmissions(oldFilter)、不得覆盖当前错误
    await act(async () => {
      rejectWithdraw(new ApiError({ status: 409, code: 'version_conflict', message: '', details: {}, requestId: null }));
      await Promise.resolve();
    });
    expect(listSubmissions.mock.calls.length).toBe(callsBeforeReject); // 未因旧 409 触发刷新
    expect(screen.queryByText(copy.settings.knowledge.submissions.versionConflict)).not.toBeInTheDocument();
    void upload;
  });
});

describe('SubmissionsLayer 查看内容下载行为', () => {
  it('下载原件而不打开 Blob 预览窗口', async () => {
    const { accessToken } = mockAuth.login('zhangsan', 'password123', 'blob-life');
    const token = `Bearer ${accessToken}`;
    const upload = await mockKnowledge.uploadDocuments(token, 'public', [
      { name: 'blob-life.md', size: 5, type: 'text/markdown', contentHash: 'hash-blob-life' },
    ], 'idem-blob-life');
    const item = upload.items[0];
    const submissionId = item && 'submission_id' in item ? (item.submission_id ?? '') : '';
    expect(submissionId).not.toBe('');
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);
    const createSpy = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:submission-download');
    const revokeSpy = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    const downloads: { href: string; filename: string }[] = [];
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function (this: HTMLAnchorElement) {
        downloads.push({ href: this.href, filename: this.download });
      });

    const api = createContractApi();
    const user = userEvent.setup();
    await renderLayer(api);

    await user.click((await screen.findAllByRole('button', { name: copy.settings.knowledge.submissions.viewContent }))[0]);
    await waitFor(() =>
      expect(downloads).toEqual([{ href: 'blob:submission-download', filename: 'blob-life.md' }]),
    );
    expect(openSpy).not.toHaveBeenCalled();
    expect(createSpy).toHaveBeenCalledTimes(1);
    expect(revokeSpy).toHaveBeenCalledWith('blob:submission-download');

    openSpy.mockRestore();
    createSpy.mockRestore();
    revokeSpy.mockRestore();
    clickSpy.mockRestore();
  });
});
