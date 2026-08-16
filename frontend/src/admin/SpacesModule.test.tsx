/*
 * 知识空间测试（§7 管理段；验收 A12、A23–A33）。
 * 读模型与成功路径经契约 mock（MockKnowledgeController / MockChatController / MockAdminController
 * 直接代理，与真实 handler 同源）；409 / 5xx 错误路径注入 ApiError（与真实 client 错误形态一致）。
 * 公共库：行操作仅服务端 permission=manage 渲染（唯一依据，非 manage 收起）、行点击新窗口
 * /preview/:id；图谱维护区仅 ops 挂载（admin 不渲染不调用）、三态文案、发起确认层
 * （revision + 预估参考）、非终态 5s 轮询到终态、取消按 allowed_actions、409/422/503 错误系列。
 * 个人库：聚合搜索防抖传 q、冻结行 aria-disabled 不可点 + tag、只读下钻（无行操作）。
 * 部门库：active 按 permission 渲染行操作、inactive 固定只读 + 页头标记，两种状态均无上传入口。
 */

import { act, fireEvent, render, screen, waitFor, within, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/errors';
import { AuthProvider } from '../auth/AuthProvider';
import type { User } from '../auth/types';
import { copy } from '../copy';
import { EscStackProvider } from '../lib/esc-stack-provider';
import { MockHttpError } from '../mocks/auth-contract';
import { mockAdmin, mockAuth, mockChat, mockKnowledge } from '../mocks/testing';
import type { NotificationsStore } from '../notifications/store';
import type { SettingsApi } from '../settings/api';
import { SettingsProvider } from '../settings/SettingsProvider';
import type { DocumentListQuery } from '../settings/types';
import { createAuthedStore, fakeAdminApi } from '../test/auth-fixtures';
import type { ThemeController } from '../theme/theme';
import { AdminProvider } from './AdminProvider';
import type { AdminApi } from './api';
import { DepartmentLibsLayer, PersonalLibsLayer, PublicSpaceLayer } from './SpacesModule';
import type { AdminUserListQuery, DepartmentStatusFilter, GraphBuildRun } from './types';

const copySpaces = copy.admin.spaces;
const copyGraph = copy.admin.spaces.graph;
const copyDocs = copy.settings.knowledge.documents;
const copyDepartments = copy.admin.departments;

/** 图谱状态行 / run 行是多段拼接文本（label · revision · …），按片段匹配。 */
function textIncluding(fragment: string): (content: string) => boolean {
  return (content) => content.includes(fragment);
}

afterEach(() => {
  // fake timers 用例失败时兜底恢复，避免泄漏污染后续用例
  vi.useRealTimers();
});

/** controller 同步抛出的 MockHttpError 归一化为 ApiError（与真实 client 错误形态一致）。 */
function call<T>(fn: () => T): Promise<T> {
  try {
    return Promise.resolve(fn());
  } catch (error) {
    if (error instanceof MockHttpError) {
      return Promise.reject(
        new ApiError({
          status: error.status,
          code: error.code,
          message: error.message,
          details: error.details,
          requestId: null,
        }),
      );
    }
    return Promise.reject(error);
  }
}

function loginToken(username: string): string {
  const { accessToken } = mockAuth.login(username, 'password123', 'vitest');
  return `Bearer ${accessToken}`;
}

function opsUser(): User {
  return {
    id: 'u_ops',
    username: 'ops-wang',
    display_name: '王运维',
    real_name: '王运维',
    department: null,
    role: 'ops',
    avatar_url: null,
  };
}

function adminUser(): User {
  return {
    id: 'u_admin',
    username: 'admin',
    display_name: '系统管理员',
    real_name: '系统管理员',
    department: null,
    role: 'admin',
    avatar_url: null,
  };
}

function contractAdminApi(token: string, overrides: Partial<AdminApi> = {}): AdminApi {
  return fakeAdminApi({
    getCurrentGraphBuild: vi.fn(() => call(() => mockAdmin.getCurrentGraphBuild(token))),
    createGraphBuild: vi.fn((revision: number, key: string) =>
      call(() => mockAdmin.createGraphBuild(token, revision, key)),
    ),
    cancelGraphBuild: vi.fn((id: string, version: number, key: string) =>
      call(() => mockAdmin.cancelGraphBuild(token, id, version, key)),
    ),
    listUsers: vi.fn((query: AdminUserListQuery) => call(() => mockAdmin.listUsers(token, query))),
    listDepartments: vi.fn((status?: DepartmentStatusFilter) =>
      call(() => mockAdmin.listDepartments(token, status)),
    ),
    ...overrides,
  });
}

function contractSettingsApi(token: string, overrides: Partial<SettingsApi> = {}): SettingsApi {
  return {
    getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })),
    listDocuments: vi.fn((input: DocumentListQuery) =>
      call(() => mockKnowledge.listDocuments(token, input.spaceId, input.q, input.page, input.pageSize)),
    ),
    listManageSpaces: vi.fn(() => call(() => ({ items: mockChat.listSpaces(token, 'manage') }))),
    deleteDocument: vi.fn((id: string, version: number, key: string) =>
      call(() => mockKnowledge.deleteDocument(token, id, version, key)),
    ),
    rebuildDocument: vi.fn((id: string, version: number, key: string) =>
      call(() => mockKnowledge.rebuildDocument(token, id, version, key)),
    ),
    ...overrides,
  } as unknown as SettingsApi;
}

async function renderSpaces(
  ui: ReactElement,
  user: User,
  adminApi: AdminApi,
  settingsApi: SettingsApi,
  store?: Awaited<ReturnType<typeof createAuthedStore>>,
): Promise<RenderResult> {
  const authStore = store ?? (await createAuthedStore(user));
  let result!: RenderResult;
  await act(async () => {
    result = render(
      <AuthProvider store={authStore}>
        <MemoryRouter initialEntries={['/']}>
          <SettingsProvider
            api={settingsApi}
            authStore={authStore}
            theme={{ setPreference: vi.fn() } as unknown as ThemeController}
            notifications={{} as NotificationsStore}
          >
            <AdminProvider api={adminApi}>
              <EscStackProvider>{ui}</EscStackProvider>
            </AdminProvider>
          </SettingsProvider>
        </MemoryRouter>
      </AuthProvider>,
    );
    await Promise.resolve();
  });
  return result;
}

function rowOf(text: string): HTMLElement {
  const row = screen.getByText(text).closest('li');
  if (row === null) {
    throw new Error(`row not found: ${text}`);
  }
  return row;
}

describe('公共库（§7.2 ops / §7.3 admin）', () => {
  it('空间文档按服务端总数提供下一页', async () => {
    const token = loginToken('ops-wang');
    const source = mockKnowledge.listDocuments(token, 'public', undefined, 1, 20).items[0]!;
    const firstPage = Array.from({ length: 20 }, (_, index) => ({
      ...source,
      id: `public-page-one-${index}`,
      name: `公共文档 ${index + 1}`,
    }));
    const secondPage = { ...source, id: 'public-page-two', name: '第二页公共文档' };
    const listDocuments = vi.fn(async (input: DocumentListQuery) => ({
      items: input.page === 2 ? [secondPage] : firstPage,
      total: 21,
      page: input.page ?? 1,
      page_size: input.pageSize ?? 20,
    }));
    const settingsApi = contractSettingsApi(token, { listDocuments });

    await renderSpaces(<PublicSpaceLayer />, opsUser(), contractAdminApi(token), settingsApi);
    const user = userEvent.setup();
    expect(await screen.findByText('公共文档 1')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: copy.controls.paginatorNext }));

    expect(await screen.findByText('第二页公共文档')).toBeInTheDocument();
    expect(listDocuments).toHaveBeenLastCalledWith({ spaceId: 'public', page: 2, pageSize: 20 });
  });

  it('permission=manage（ops）：行操作菜单渲染版本记录 / 重建索引 / 上传新版本 / 删除', async () => {
    const token = loginToken('ops-wang');
    await renderSpaces(
      <PublicSpaceLayer />,
      opsUser(),
      contractAdminApi(token),
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    expect(await screen.findByText('公共制度汇编.pdf')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: copyDocs.rowMenuAria('公共制度汇编.pdf') }));
    expect(await screen.findByRole('menuitem', { name: copyDocs.versions })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: copyDocs.reindex })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: copyDocs.uploadNewVersion })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: copyDocs.delete })).toBeInTheDocument();
  });

  it('permission 非 manage：行操作整体收起（唯一依据为服务端 permission）', async () => {
    const token = loginToken('ops-wang');
    const settingsApi = contractSettingsApi(token, {
      listManageSpaces: vi.fn(async () => ({
        items: [
          { id: 'public', kind: 'public' as const, name: '公共库', permission: 'read' as const, document_count: 1 },
        ],
      })),
    });
    await renderSpaces(<PublicSpaceLayer />, opsUser(), contractAdminApi(token), settingsApi);
    expect(await screen.findByText('公共制度汇编.pdf')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: copyDocs.rowMenuAria('公共制度汇编.pdf') }),
    ).toBeNull();
  });

  it('行点击新窗口打开 /preview/:documentId 只读形态', async () => {
    const token = loginToken('ops-wang');
    const documentId = mockKnowledge.listDocuments(token, 'public', undefined, 1, 20).items[0]!.id;
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    try {
      await renderSpaces(
        <PublicSpaceLayer />,
        opsUser(),
        contractAdminApi(token),
        contractSettingsApi(token),
      );
      const user = userEvent.setup();
      await user.click(
        await screen.findByRole('button', { name: copySpaces.openPreviewAria('公共制度汇编.pdf') }),
      );
      expect(openSpy).toHaveBeenCalledWith(
        `/preview/${encodeURIComponent(documentId)}`,
        '_blank',
        'noopener,noreferrer',
      );
    } finally {
      openSpy.mockRestore();
    }
  });

  it('图谱维护区仅 ops 挂载；admin 不渲染且不发起图谱读', async () => {
    const opsToken = loginToken('ops-wang');
    const opsAdminApi = contractAdminApi(opsToken);
    const opsRender = await renderSpaces(
      <PublicSpaceLayer />,
      opsUser(),
      opsAdminApi,
      contractSettingsApi(opsToken),
    );
    expect(await screen.findByText(copyGraph.title)).toBeInTheDocument();
    await waitFor(() => expect(opsAdminApi.getCurrentGraphBuild).toHaveBeenCalled());
    // 同用例内再次 render 前卸载，避免 DOM 叠加
    opsRender.unmount();

    const adminToken = loginToken('admin');
    const adminApi = contractAdminApi(adminToken);
    await renderSpaces(
      <PublicSpaceLayer />,
      adminUser(),
      adminApi,
      contractSettingsApi(adminToken),
    );
    // 文档列表加载落定后断言：图谱区不渲染、图谱读未调用
    expect(await screen.findByText('公共制度汇编.pdf')).toBeInTheDocument();
    expect(screen.queryByText(copyGraph.title)).toBeNull();
    expect(adminApi.getCurrentGraphBuild).not.toHaveBeenCalled();
  });
});

describe('图谱维护区（§6.12，ops）', () => {
  async function renderGraphSection(token: string, adminApi?: AdminApi) {
    await renderSpaces(
      <PublicSpaceLayer />,
      opsUser(),
      adminApi ?? contractAdminApi(token),
      contractSettingsApi(token),
    );
    return screen.findByText(copyGraph.title);
  }

  it('disabled 态：「图谱未构建」+ 构建图谱入口 + 无构建记录', async () => {
    mockAdmin.setGraphProjection({ availability: 'disabled', activeGeneration: null });
    const token = loginToken('ops-wang');
    await renderGraphSection(token);
    expect(await screen.findByText(textIncluding(copyGraph.availabilityDisabled))).toBeInTheDocument();
    expect(screen.getByRole('button', { name: copyGraph.buildCreate })).toBeInTheDocument();
    expect(screen.getByText(copyGraph.empty)).toBeInTheDocument();
  });

  it('stale 态：「图谱需重建」+ 源版本 + 过期标注（旧 generation 不展示为可用）', async () => {
    const token = loginToken('ops-wang');
    await renderGraphSection(token);
    expect(await screen.findByText(textIncluding(copyGraph.availabilityStale))).toBeInTheDocument();
    expect(screen.getByText(textIncluding(copyGraph.sourceRevision(12)))).toBeInTheDocument();
    expect(screen.getByText(copyGraph.generationExpired)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: copyGraph.buildRebuild })).toBeInTheDocument();
  });

  it('ready 态：「图谱可用」+ generation 信息，无过期标注', async () => {
    mockAdmin.setGraphProjection({
      availability: 'ready',
      activeGeneration: {
        graph_generation_id: 'gg_ready_1',
        source_revision: 12,
        built_at: '2026-07-30T08:00:00Z',
      },
    });
    const token = loginToken('ops-wang');
    await renderGraphSection(token);
    expect(await screen.findByText(textIncluding(copyGraph.availabilityReady))).toBeInTheDocument();
    expect(screen.getByText((content) => content.includes('gg_ready_1'))).toBeInTheDocument();
    expect(screen.queryByText(copyGraph.generationExpired)).toBeNull();
  });

  it('发起确认层展示 revision 与预估参考；提交后非终态 5s 轮询直至终态', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    const store = await createAuthedStore(opsUser());
    vi.useFakeTimers();
    try {
      await renderSpaces(<PublicSpaceLayer />, opsUser(), adminApi, contractSettingsApi(token), store);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      // stale 态发起重建（fireEvent 同步触发，不依赖 fake timers 下的 userEvent 延迟）
      fireEvent.click(screen.getByRole('button', { name: copyGraph.buildRebuild }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      const dialog = screen.getByRole('dialog', { name: copyGraph.confirmTitleRebuild });
      expect(within(dialog).getByText(copyGraph.confirmRevision(12))).toBeInTheDocument();
      // 无历史 run：预估说明为「提交后服务端计算」
      expect(within(dialog).getByText(copyGraph.confirmEstimatePending)).toBeInTheDocument();
      fireEvent.click(within(dialog).getByRole('button', { name: copyGraph.confirmStart }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      // 创建成功 + 首次投影（queued → running）：非终态隐藏发起按钮、呈现取消
      expect(screen.getByText(textIncluding(copyGraph.statusRunning))).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: copyGraph.buildRebuild })).toBeNull();
      expect(screen.getByRole('button', { name: copyGraph.cancel })).toBeInTheDocument();
      // 5s 轮询一拍：running → succeeded，availability 转 ready
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });
      expect(screen.getByText(textIncluding(copyGraph.statusSucceeded))).toBeInTheDocument();
      expect(screen.getByText(textIncluding(copyGraph.availabilityReady))).toBeInTheDocument();
      expect(screen.getByRole('button', { name: copyGraph.buildRebuild })).toBeInTheDocument();
      // 有历史 run：确认层展示上次预估参考
      fireEvent.click(screen.getByRole('button', { name: copyGraph.buildRebuild }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      const dialogAgain = screen.getByRole('dialog', { name: copyGraph.confirmTitleRebuild });
      expect(
        within(dialogAgain).getByText(copyGraph.confirmEstimate(3)),
      ).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('取消按 allowed_actions 渲染；取消成功轻提示并回到无进行中状态', async () => {
    const token = loginToken('ops-wang');
    await renderSpaces(
      <PublicSpaceLayer />,
      opsUser(),
      contractAdminApi(token),
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await screen.findByText(textIncluding(copyGraph.availabilityStale));
    await user.click(screen.getByRole('button', { name: copyGraph.buildRebuild }));
    const dialog = await screen.findByRole('dialog', { name: copyGraph.confirmTitleRebuild });
    await user.click(within(dialog).getByRole('button', { name: copyGraph.confirmStart }));
    // 非终态（queued → running）：取消入口按 allowed_actions 呈现
    expect(await screen.findByText(textIncluding(copyGraph.statusRunning))).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: copyGraph.cancel }));
    expect(await screen.findByText(copyGraph.cancelledNotice)).toBeInTheDocument();
    // run 行状态（「最近一次构建 · 已取消 · …」），与轻提示「已取消构建」区分
    expect(
      await screen.findByText(
        (content) => content.includes(copyGraph.latestRunTitle) && content.includes(copyGraph.statusCancelled),
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: copyGraph.cancel })).toBeNull();
  });

  it('409 graph_source_changed：关框 + 错误行 + 状态刷新（最新 revision）', async () => {
    const token = loginToken('ops-wang');
    await renderSpaces(
      <PublicSpaceLayer />,
      opsUser(),
      contractAdminApi(token),
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await screen.findByText(textIncluding(copyGraph.sourceRevision(12)));
    // 确认层打开前公共库内容已变化（revision 12 → 13）
    mockAdmin.setGraphProjection({ availability: 'stale', sourceRevision: 13 });
    await user.click(screen.getByRole('button', { name: copyGraph.buildRebuild }));
    const dialog = await screen.findByRole('dialog', { name: copyGraph.confirmTitleRebuild });
    await user.click(within(dialog).getByRole('button', { name: copyGraph.confirmStart }));
    expect(await screen.findByText(copyGraph.sourceChanged)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(await screen.findByText(textIncluding(copyGraph.sourceRevision(13)))).toBeInTheDocument();
  });

  it('409 graph_build_in_progress：关框 + 错误行 + 状态刷新', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token, {
      createGraphBuild: vi.fn(async () => {
        throw new ApiError({
          status: 409,
          code: 'graph_build_in_progress',
          message: '',
          details: {},
          requestId: null,
        });
      }),
    });
    await renderSpaces(
      <PublicSpaceLayer />,
      opsUser(),
      adminApi,
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await screen.findByText(textIncluding(copyGraph.availabilityStale));
    await user.click(screen.getByRole('button', { name: copyGraph.buildRebuild }));
    const dialog = await screen.findByRole('dialog', { name: copyGraph.confirmTitleRebuild });
    await user.click(within(dialog).getByRole('button', { name: copyGraph.confirmStart }));
    expect(await screen.findByText(copyGraph.inProgress)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('422 graph_source_empty：关框 + 错误行', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token, {
      createGraphBuild: vi.fn(async () => {
        throw new ApiError({
          status: 422,
          code: 'graph_source_empty',
          message: '',
          details: {},
          requestId: null,
        });
      }),
    });
    await renderSpaces(
      <PublicSpaceLayer />,
      opsUser(),
      adminApi,
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await screen.findByText(textIncluding(copyGraph.availabilityStale));
    await user.click(screen.getByRole('button', { name: copyGraph.buildRebuild }));
    const dialog = await screen.findByRole('dialog', { name: copyGraph.confirmTitleRebuild });
    await user.click(within(dialog).getByRole('button', { name: copyGraph.confirmStart }));
    expect(await screen.findByText(copyGraph.sourceEmpty)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('503 graph_build_estimate_unavailable：错误行 + 重试文字链重新打开发起确认层', async () => {
    const token = loginToken('ops-wang');
    mockAdmin.setGraphEstimateAvailable(false);
    await renderSpaces(
      <PublicSpaceLayer />,
      opsUser(),
      contractAdminApi(token),
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await screen.findByText(textIncluding(copyGraph.availabilityStale));
    await user.click(screen.getByRole('button', { name: copyGraph.buildRebuild }));
    const dialog = await screen.findByRole('dialog', { name: copyGraph.confirmTitleRebuild });
    await user.click(within(dialog).getByRole('button', { name: copyGraph.confirmStart }));
    expect(await screen.findByText(copyGraph.estimateUnavailable)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    // 重试文字链：重新打开发起确认层（用户显式重发）
    await user.click(screen.getByRole('button', { name: copy.states.retry }));
    expect(await screen.findByRole('dialog', { name: copyGraph.confirmTitleRebuild })).toBeInTheDocument();
  });

  it('取消 409 graph_build_not_cancellable：错误行 + 状态刷新', async () => {
    const token = loginToken('ops-wang');
    const runningRun: GraphBuildRun = {
      graph_build_id: 'gb_running_1',
      version: 2,
      status: 'running',
      source_revision: 12,
      estimated_primary_model_calls: 3,
      actual_usage: null,
      created_at: '2026-08-01T00:00:00Z',
      started_at: '2026-08-01T00:00:05Z',
      finished_at: null,
      failure_class: null,
      allowed_actions: ['cancel'],
    };
    const getCurrentGraphBuild = vi.fn(async () => ({
      space_id: 'public' as const,
      source_revision: 12,
      graph_availability: 'stale' as const,
      active_generation: null,
      latest_run: runningRun,
    }));
    const adminApi = contractAdminApi(token, {
      getCurrentGraphBuild,
      cancelGraphBuild: vi.fn(async () => {
        throw new ApiError({
          status: 409,
          code: 'graph_build_not_cancellable',
          message: '',
          details: {},
          requestId: null,
        });
      }),
    });
    await renderSpaces(
      <PublicSpaceLayer />,
      opsUser(),
      adminApi,
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await screen.findByText(textIncluding(copyGraph.statusRunning));
    await user.click(screen.getByRole('button', { name: copyGraph.cancel }));
    expect(await screen.findByText(copyGraph.notCancellable)).toBeInTheDocument();
    await waitFor(() => expect(getCurrentGraphBuild.mock.calls.length).toBeGreaterThan(1));
  });
});

describe('用户个人库（§7.3）', () => {
  it('个人库用户按服务端总数提供下一页', async () => {
    const token = loginToken('admin');
    const source = mockAdmin.listUsers(token, { page: 1, pageSize: 50 }).items[0]!;
    const firstPage = Array.from({ length: 50 }, (_, index) => ({
      ...source,
      id: `personal-page-one-${index}`,
      display_name: `个人库用户 ${index + 1}`,
      username: `personal-${index + 1}`,
    }));
    const secondPage = {
      ...source,
      id: 'personal-page-two',
      display_name: '第二页个人库用户',
      username: 'personal-page-two',
    };
    const listUsers = vi.fn(async (input: AdminUserListQuery) => ({
      items: input.page === 2 ? [secondPage] : firstPage,
      total: 51,
      page: input.page ?? 1,
      page_size: input.pageSize ?? 50,
    }));
    const adminApi = contractAdminApi(token, { listUsers });

    await renderSpaces(<PersonalLibsLayer />, adminUser(), adminApi, contractSettingsApi(token));
    const user = userEvent.setup();
    expect(await screen.findByText('个人库用户 1')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: copy.controls.paginatorNext }));

    expect(await screen.findByText('第二页个人库用户')).toBeInTheDocument();
    // 首次空搜索不应在 300ms 防抖到期后将用户从第 2 页重置回第 1 页。
    await new Promise<void>((resolve) => window.setTimeout(resolve, 350));
    expect(adminApi.listUsers).toHaveBeenLastCalledWith({ q: undefined, page: 2, pageSize: 50 });
  });

  it('用户列表 + 顶部聚合搜索：防抖传 q，实时过滤（部门名命中）', async () => {
    const token = loginToken('admin');
    const adminApi = contractAdminApi(token);
    await renderSpaces(
      <PersonalLibsLayer />,
      adminUser(),
      adminApi,
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    expect(await screen.findByText('陈晨')).toBeInTheDocument();
    expect(screen.getByText('鬼影')).toBeInTheDocument();
    await user.type(screen.getByRole('searchbox', { name: copySpaces.userSearchAria }), '人事');
    // 防抖 300ms 后传 q 重新请求；命中部门名「人事部」的陈晨 / 孙琪
    await waitFor(() => expect(screen.queryByText('鬼影')).not.toBeInTheDocument(), {
      timeout: 2000,
    });
    await waitFor(() =>
      expect(adminApi.listUsers).toHaveBeenCalledWith(
        expect.objectContaining({ q: '人事', page: 1, pageSize: 50 }),
      ),
    );
    expect(screen.getByText('陈晨')).toBeInTheDocument();
    expect(screen.getByText('孙琪')).toBeInTheDocument();
  });

  it('冻结行（pending_delete）保留可见但不可点击 + 行尾冻结 tag', async () => {
    const token = loginToken('admin');
    await renderSpaces(
      <PersonalLibsLayer />,
      adminUser(),
      contractAdminApi(token),
      contractSettingsApi(token),
    );
    await screen.findByText('鬼影');
    const ghostRow = rowOf('鬼影');
    expect(ghostRow.getAttribute('aria-disabled')).toBe('true');
    expect(within(ghostRow).getByText(copy.admin.common.frozenTag)).toBeInTheDocument();
    expect(within(ghostRow).queryByRole('button')).toBeNull();
  });

  it('下钻：只读文档列表（页头只读标记、无行操作、无上传入口）；返回恢复列表', async () => {
    const token = loginToken('admin');
    await renderSpaces(
      <PersonalLibsLayer />,
      adminUser(),
      contractAdminApi(token),
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await screen.findByText('陈晨');
    await user.click(within(rowOf('陈晨')).getByRole('button'));
    // 页头：个人库标题 + 只读标记
    expect(await screen.findByText(copySpaces.personalLibOf('陈晨'))).toBeInTheDocument();
    expect(screen.getByText(copy.admin.common.readOnly)).toBeInTheDocument();
    // 只读文档列表（knowledge 种子 2 篇）：无行操作、无上传入口
    expect(await screen.findByText('入职培训笔记.md')).toBeInTheDocument();
    expect(screen.getByText('人事政策摘编.pdf')).toBeInTheDocument();
    const table = screen.getByRole('table', { name: copyDocs.title });
    expect(within(table).getAllByRole('columnheader')).toHaveLength(4);
    const documentRow = within(table).getByRole('row', { name: /入职培训笔记\.md/ });
    expect(within(documentRow).getAllByRole('cell')).toHaveLength(4);
    expect(
      screen.queryByRole('button', { name: copyDocs.rowMenuAria('入职培训笔记.md') }),
    ).toBeNull();
    // 返回用户列表
    await user.click(screen.getByRole('button', { name: copySpaces.backToUsers }));
    expect(await screen.findByText('鬼影')).toBeInTheDocument();
  });
});

describe('部门库（§7.3）', () => {
  it('部门列表：名称 / 成员·文档数 / 状态 / 待审徽标；inactive 行已停用标记', async () => {
    const token = loginToken('admin');
    await renderSpaces(
      <DepartmentLibsLayer />,
      adminUser(),
      contractAdminApi(token),
      contractSettingsApi(token),
    );
    expect(await screen.findByText('财务部')).toBeInTheDocument();
    expect(screen.getByText('人事部')).toBeInTheDocument();
    expect(screen.getByText('空壳部')).toBeInTheDocument();
    const financeRow = rowOf('财务部');
    expect(
      within(financeRow).getByText(
        (content) =>
          content.includes(copySpaces.members(3)) && content.includes(copySpaces.documents(2)),
      ),
    ).toBeInTheDocument();
    // 待审徽标（pending_submission_count=2）
    expect(within(financeRow).getByText('2')).toBeInTheDocument();
    const legacyRow = rowOf('档案部');
    expect(within(legacyRow).getByText(copyDepartments.statusInactive)).toBeInTheDocument();
  });

  it('active 部门下钻：按服务端 permission 渲染行操作（admin=manage）', async () => {
    const token = loginToken('admin');
    await renderSpaces(
      <DepartmentLibsLayer />,
      adminUser(),
      contractAdminApi(token),
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await screen.findByText('财务部');
    await user.click(within(rowOf('财务部')).getByRole('button'));
    expect(await screen.findByText('财务审批流程.pdf')).toBeInTheDocument();
    expect(screen.getByText('差旅报销标准.docx')).toBeInTheDocument();
    // permission=manage → 行操作呈现
    expect(
      screen.getByRole('button', { name: copyDocs.rowMenuAria('财务审批流程.pdf') }),
    ).toBeInTheDocument();
    // active + manage 时页头不显示「只读」标记（Verifier minor：只读标记与行操作不并存）
    expect(screen.queryByText(copy.admin.common.readOnly)).toBeNull();
    // 返回部门列表
    await user.click(screen.getByRole('button', { name: copySpaces.backToDepartments }));
    expect(await screen.findByText('人事部')).toBeInTheDocument();
  });

  it('inactive 部门下钻：固定只读 + 页头「已停用，只读」标记，无行操作无上传入口', async () => {
    const token = loginToken('admin');
    await renderSpaces(
      <DepartmentLibsLayer />,
      adminUser(),
      contractAdminApi(token),
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await screen.findByText('档案部');
    await user.click(within(rowOf('档案部')).getByRole('button'));
    expect(await screen.findByText(copy.admin.common.deactivatedReadOnly)).toBeInTheDocument();
    expect(await screen.findByText('2019 年档案汇编.pdf')).toBeInTheDocument();
    expect(screen.getByText('档案借阅登记.xlsx')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: copyDocs.rowMenuAria('2019 年档案汇编.pdf') }),
    ).toBeNull();
  });

  it('空部门下钻：文档空态', async () => {
    const token = loginToken('admin');
    await renderSpaces(
      <DepartmentLibsLayer />,
      adminUser(),
      contractAdminApi(token),
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await screen.findByText('空壳部');
    await user.click(within(rowOf('空壳部')).getByRole('button'));
    expect(await screen.findByText(copyDocs.empty)).toBeInTheDocument();
  });
});
