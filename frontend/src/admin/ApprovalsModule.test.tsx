/*
 * 审批中心测试（§8；验收 A2、A20–A22、A34）。
 * 读模型与成功路径经契约 mock（MockAdminController / MockKnowledgeController 直接代理，
 * 与真实 handler 同源）；409 / 网络错误路径注入 ApiError（与真实 client 归一化错误形态一致）。
 * 配额：四列 / 批准对话框（approved_pages 校验与缺省）/ 驳回无输入框浮层 / 成功淡出 +
 * 成功轻提示 + invalidateSummaries 徽标刷新 / 409 系列（version_conflict 对话框内重确认、
 * already_processed、quota_request_not_approvable）/ 处理中禁用。
 * 投稿：最小列表字段 / 查看内容受控窗 / 驳回原因随请求 / approve 202 淡出 /
 * duplicate_document 行内不移除 / version_conflict 行内 + 刷新 /
 * scope_changed·submitter_pending_delete 刷新。
 */

import { act, render, screen, waitFor, within, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/errors';
import { AuthProvider } from '../auth/AuthProvider';
import type { User } from '../auth/types';
import { copy } from '../copy';
import { EscStackProvider } from '../lib/esc-stack-provider';
import { MockHttpError } from '../mocks/auth-contract';
import { mockAdmin, mockAuth, mockKnowledge } from '../mocks/testing';
import type { NotificationsStore } from '../notifications/store';
import type { SettingsApi } from '../settings/api';
import { SettingsProvider } from '../settings/SettingsProvider';
import { createAuthedStore, fakeAdminApi } from '../test/auth-fixtures';
import type { ThemeController } from '../theme/theme';
import { AdminProvider } from './AdminProvider';
import type { AdminApi } from './api';
import { ApprovalSubmissionsLayer, QuotaRequestsLayer } from './ApprovalsModule';
import { QuotaRequestsSummaryBadge } from './summaries';
import type { AdminUserListQuery, DepartmentStatusFilter, QuotaRequestStatus } from './types';

const copyApprovals = copy.admin.approvals;
const copyManage = copy.settings.knowledge.manage;

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

/** 经契约 mock 的 AdminApi：读/写直接代理 controller（与真实 handler 同源），可按用例覆盖。 */
function contractAdminApi(token: string, overrides: Partial<AdminApi> = {}): AdminApi {
  return fakeAdminApi({
    getApprovalSummary: vi.fn(() => call(() => mockAdmin.getApprovalSummary(token))),
    listQuotaRequests: vi.fn((status?: QuotaRequestStatus) =>
      call(() => mockAdmin.listQuotaRequests(token, status)),
    ),
    approveQuotaRequest: vi.fn((id: string, version: number, pages: number | null, key: string) =>
      call(() => mockAdmin.approveQuotaRequest(token, id, version, pages, key)),
    ),
    rejectQuotaRequest: vi.fn((id: string, version: number, key: string) =>
      call(() => mockAdmin.rejectQuotaRequest(token, id, version, key)),
    ),
    listApprovalSubmissions: vi.fn(() => call(() => mockKnowledge.listApprovals(token))),
    approveSubmission: vi.fn((id: string, version: number, key: string) =>
      call(() => mockKnowledge.approveSubmission(token, id, version, key)),
    ),
    rejectSubmission: vi.fn((id: string, version: number, reason: string | null, key: string) =>
      call(() => mockKnowledge.rejectSubmission(token, id, version, reason, key)),
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
    getSubmissionContent: vi.fn((submissionId: string) =>
      call(() => {
        const content = mockKnowledge.getSubmissionContent(token, submissionId);
        return new Blob([content.bytes as BlobPart], { type: content.type });
      }),
    ),
    ...overrides,
  } as unknown as SettingsApi;
}

async function renderApprovals(
  ui: ReactElement,
  user: User,
  adminApi: AdminApi,
  settingsApi: SettingsApi,
): Promise<RenderResult> {
  const store = await createAuthedStore(user);
  let result!: RenderResult;
  await act(async () => {
    result = render(
      <AuthProvider store={store}>
        <MemoryRouter initialEntries={['/']}>
          <SettingsProvider
            api={settingsApi}
            authStore={store}
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

function pendingQuotaRequest(token: string, applicantName: string) {
  const item = mockAdmin
    .listQuotaRequests(token, 'pending')
    .items.find((request) => request.applicant.display_name === applicantName);
  if (item === undefined) {
    throw new Error(`pending quota request not found: ${applicantName}`);
  }
  return item;
}

function pendingSubmission(token: string, name: string) {
  const item = mockKnowledge.listApprovals(token).items.find((submission) => submission.name === name);
  if (item === undefined) {
    throw new Error(`pending submission not found: ${name}`);
  }
  return item;
}

describe('配额申请（§8.2–8.3，ops）', () => {
  it('四列渲染：申请人 / 当前用量 / 申请量 / 申请时间 + 批准、驳回小 pill', async () => {
    const token = loginToken('ops-wang');
    await renderApprovals(
      <QuotaRequestsLayer />,
      opsUser(),
      contractAdminApi(token),
      contractSettingsApi(token),
    );

    expect(await screen.findByText('zhangsan')).toBeInTheDocument();
    expect(screen.getByText('minister-li')).toBeInTheDocument();
    expect(screen.getByText('ghost')).toBeInTheDocument();
    // 当前用量（zhangsan / minister-li 均 120 / 500）与申请量
    expect(screen.getAllByText(copyApprovals.usageOf(120, 500)).length).toBe(2);
    expect(screen.getByText(copyApprovals.usageOf(0, 500))).toBeInTheDocument();
    expect(screen.getByText(copyApprovals.pages(100))).toBeInTheDocument();
    expect(screen.getByText(copyApprovals.pages(200))).toBeInTheDocument();
    expect(screen.getByText(copyApprovals.pages(50))).toBeInTheDocument();
    const table = screen.getByRole('table', { name: copyApprovals.quota });
    expect(within(table).getAllByRole('columnheader')).toHaveLength(5);
    const semanticRow = within(table).getByRole('row', { name: /zhangsan/ });
    expect(within(semanticRow).getAllByRole('cell')).toHaveLength(5);
    const row = rowOf('zhangsan');
    expect(within(row).getByRole('button', { name: copyApprovals.approve })).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: copyApprovals.reject })).toBeInTheDocument();
  });

  it('批准缺省按申请量：成功淡出 + 成功轻提示 + invalidateSummaries 徽标刷新', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    const target = pendingQuotaRequest(token, 'zhangsan');
    await renderApprovals(
      <>
        <QuotaRequestsSummaryBadge />
        <QuotaRequestsLayer />
      </>,
      opsUser(),
      adminApi,
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    // 徽标初始 = pending 3 条
    expect(await screen.findByText('3')).toBeInTheDocument();
    await user.click(within(rowOf('zhangsan')).getByRole('button', { name: copyApprovals.approve }));
    const dialog = await screen.findByRole('dialog', { name: copyApprovals.approveDialogTitle });
    await user.click(within(dialog).getByRole('button', { name: copy.controls.confirm }));

    await waitFor(() =>
      expect(adminApi.approveQuotaRequest).toHaveBeenCalledWith(
        target.id,
        target.version,
        null,
        expect.stringMatching(/^idem_/),
      ),
    );
    expect(await screen.findByText(copyApprovals.approvedNotice)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('zhangsan')).not.toBeInTheDocument());
    // invalidateSummaries → 徽标重取：3 → 2
    expect(await screen.findByText('2')).toBeInTheDocument();
  });

  it('approved_pages 校验：非法输入红边 + danger 说明 + 确认禁用；合法值随请求提交', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    const target = pendingQuotaRequest(token, 'zhangsan');
    await renderApprovals(
      <QuotaRequestsLayer />,
      opsUser(),
      adminApi,
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await user.click(within(rowOf('zhangsan')).getByRole('button', { name: copyApprovals.approve }));
    const dialog = await screen.findByRole('dialog', { name: copyApprovals.approveDialogTitle });
    const input = within(dialog).getByRole('textbox', { name: copyApprovals.approvePagesLabel });
    const confirm = within(dialog).getByRole('button', { name: copy.controls.confirm }) as HTMLButtonElement;

    await user.type(input, '0');
    expect(await within(dialog).findByText(copyApprovals.approvePagesInvalid(100))).toBeInTheDocument();
    expect(confirm.disabled).toBe(true);
    expect(input.getAttribute('aria-invalid')).toBe('true');

    await user.clear(input);
    await user.type(input, '101');
    expect(await within(dialog).findByText(copyApprovals.approvePagesInvalid(100))).toBeInTheDocument();
    expect(confirm.disabled).toBe(true);

    await user.clear(input);
    await user.type(input, '50');
    await waitFor(() => expect(confirm.disabled).toBe(false));
    expect(within(dialog).queryByText(copyApprovals.approvePagesInvalid(100))).toBeNull();
    await user.click(confirm);
    await waitFor(() =>
      expect(adminApi.approveQuotaRequest).toHaveBeenCalledWith(
        target.id,
        target.version,
        50,
        expect.stringMatching(/^idem_/),
      ),
    );
  });

  it('配额批准框在有效页数后以 Enter 提交', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    const target = pendingQuotaRequest(token, 'zhangsan');
    await renderApprovals(
      <QuotaRequestsLayer />,
      opsUser(),
      adminApi,
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await user.click(within(rowOf('zhangsan')).getByRole('button', { name: copyApprovals.approve }));
    const dialog = await screen.findByRole('dialog', { name: copyApprovals.approveDialogTitle });
    await user.type(within(dialog).getByRole('textbox', { name: copyApprovals.approvePagesLabel }), '50');

    await user.keyboard('{Enter}');

    await waitFor(() =>
      expect(adminApi.approveQuotaRequest).toHaveBeenCalledWith(
        target.id,
        target.version,
        50,
        expect.stringMatching(/^idem_/),
      ),
    );
  });

  it('驳回为无输入框的危险确认浮层；成功淡出 + 轻提示', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    const target = pendingQuotaRequest(token, 'minister-li');
    await renderApprovals(
      <QuotaRequestsLayer />,
      opsUser(),
      adminApi,
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await user.click(within(rowOf('minister-li')).getByRole('button', { name: copyApprovals.reject }));
    const dialog = await screen.findByRole('dialog', { name: copyApprovals.rejectDialogTitle });
    expect(within(dialog).queryByRole('textbox')).toBeNull();
    await user.click(within(dialog).getByRole('button', { name: copyApprovals.reject }));

    await waitFor(() =>
      expect(adminApi.rejectQuotaRequest).toHaveBeenCalledWith(
        target.id,
        target.version,
        expect.stringMatching(/^idem_/),
      ),
    );
    expect(await screen.findByText(copyApprovals.rejectedNotice)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('minister-li')).not.toBeInTheDocument());
  });

  it('409 quota_request_not_approvable（冻结申请人种子）：关框 + 页头说明 + 刷新后行仍在', async () => {
    const token = loginToken('ops-wang');
    await renderApprovals(
      <QuotaRequestsLayer />,
      opsUser(),
      contractAdminApi(token),
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await user.click(within(rowOf('ghost')).getByRole('button', { name: copyApprovals.approve }));
    const dialog = await screen.findByRole('dialog', { name: copyApprovals.approveDialogTitle });
    await user.click(within(dialog).getByRole('button', { name: copy.controls.confirm }));

    expect(await screen.findByText(copyApprovals.notApprovable)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    // 刷新后该申请仍 pending：行保留
    expect(screen.getByText('ghost')).toBeInTheDocument();
  });

  it('409 already_processed（他人已处理）：关框 + 页头说明 + 行随刷新消失', async () => {
    const token = loginToken('ops-wang');
    const target = pendingQuotaRequest(token, 'zhangsan');
    await renderApprovals(
      <QuotaRequestsLayer />,
      opsUser(),
      contractAdminApi(token),
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await user.click(within(rowOf('zhangsan')).getByRole('button', { name: copyApprovals.approve }));
    const dialog = await screen.findByRole('dialog', { name: copyApprovals.approveDialogTitle });
    // 另一审核者先处理（经 controller 直接批准，申请转 approved）
    mockAdmin.approveQuotaRequest(token, target.id, target.version, null, 'idem-other-reviewer');
    await user.click(within(dialog).getByRole('button', { name: copy.controls.confirm }));

    expect(await screen.findByText(copyApprovals.alreadyProcessed)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(screen.queryByText('zhangsan')).not.toBeInTheDocument());
  });

  it('409 version_conflict 且行仍在：对话框内顶部说明 + 换最新行重新确认', async () => {
    const token = loginToken('ops-wang');
    let conflict = true;
    const adminApi = contractAdminApi(token, {
      approveQuotaRequest: vi.fn((id: string, version: number, pages: number | null, key: string) => {
        if (conflict) {
          conflict = false;
          return Promise.reject(
            new ApiError({ status: 409, code: 'version_conflict', message: '', details: {}, requestId: null }),
          );
        }
        return call(() => mockAdmin.approveQuotaRequest(token, id, version, pages, key));
      }),
    });
    await renderApprovals(
      <QuotaRequestsLayer />,
      opsUser(),
      adminApi,
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await user.click(within(rowOf('zhangsan')).getByRole('button', { name: copyApprovals.approve }));
    const dialog = await screen.findByRole('dialog', { name: copyApprovals.approveDialogTitle });
    await user.click(within(dialog).getByRole('button', { name: copy.controls.confirm }));

    // 对话框保留，内顶部说明（role=status）
    expect(await within(dialog).findByText(copyApprovals.versionConflict)).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: copyApprovals.approveDialogTitle })).toBeInTheDocument();
  });

  it('写操作处理中：行 aria-busy + 对话框确认禁用（防重复提交）', async () => {
    const token = loginToken('ops-wang');
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const adminApi = contractAdminApi(token, {
      approveQuotaRequest: vi.fn(async (id: string, version: number, pages: number | null, key: string) => {
        await gate;
        return mockAdmin.approveQuotaRequest(token, id, version, pages, key);
      }),
    });
    await renderApprovals(
      <QuotaRequestsLayer />,
      opsUser(),
      adminApi,
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await user.click(within(rowOf('zhangsan')).getByRole('button', { name: copyApprovals.approve }));
    const dialog = await screen.findByRole('dialog', { name: copyApprovals.approveDialogTitle });
    const confirm = within(dialog).getByRole('button', { name: copy.controls.confirm }) as HTMLButtonElement;
    await user.click(confirm);

    await waitFor(() => expect(confirm.disabled).toBe(true));
    await waitFor(() => expect(rowOf('zhangsan').getAttribute('aria-busy')).toBe('true'));
    // 收尾：放行完成，避免悬空写状态
    release();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });
});

describe('投稿审核（§8.4–8.5）', () => {
  it('最小审批行渲染（ops 仅公共库范围，无范围分段控件）', async () => {
    const token = loginToken('ops-wang');
    await renderApprovals(
      <ApprovalSubmissionsLayer />,
      opsUser(),
      contractAdminApi(token),
      contractSettingsApi(token),
    );

    expect(await screen.findByText('行业研报汇总.pdf')).toBeInTheDocument();
    expect(screen.getByText('公共制度汇编.pdf')).toBeInTheDocument();
    expect(screen.getByText('跨部门协作指引.pdf')).toBeInTheDocument();
    expect(screen.getByText('历史遗留材料.pdf')).toBeInTheDocument();
    const table = screen.getByRole('table', { name: copyApprovals.submissions });
    // 六列（超管端 §7.3 行结构；ops 范围仅公共库，不渲染目标空间筛选分段控件）
    expect(within(table).getAllByRole('columnheader')).toHaveLength(6);
    const semanticRow = within(table).getByRole('row', { name: /行业研报汇总\.pdf/ });
    expect(within(semanticRow).getAllByRole('cell')).toHaveLength(6);
    const row = rowOf('行业研报汇总.pdf');
    expect(within(row).getByText('pdf')).toBeInTheDocument();
    expect(within(row).getByText(/zhangsan/)).toBeInTheDocument();
    expect(within(row).getByText('公共库')).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: copyManage.viewContent })).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: copyManage.approve })).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: copyManage.reject })).toBeInTheDocument();
    // ops 待审恒为公共库：目标空间筛选分段控件不渲染
    expect(screen.queryByRole('radiogroup')).toBeNull();
  });

  it('通过 202：行淡出 + 成功轻提示', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    const target = pendingSubmission(token, '行业研报汇总.pdf');
    await renderApprovals(
      <ApprovalSubmissionsLayer />,
      opsUser(),
      adminApi,
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await user.click(within(rowOf('行业研报汇总.pdf')).getByRole('button', { name: copyManage.approve }));

    await waitFor(() =>
      expect(adminApi.approveSubmission).toHaveBeenCalledWith(
        target.submission_id,
        target.version,
        expect.stringMatching(/^idem_/),
      ),
    );
    expect(await screen.findByText(copyManage.approvedNotice)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('行业研报汇总.pdf')).not.toBeInTheDocument());
  });

  it('409 duplicate_document：行内提示，行不移除不刷新', async () => {
    const token = loginToken('ops-wang');
    await renderApprovals(
      <ApprovalSubmissionsLayer />,
      opsUser(),
      contractAdminApi(token),
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await user.click(within(rowOf('公共制度汇编.pdf')).getByRole('button', { name: copyManage.approve }));

    expect(await screen.findByText(copyManage.duplicateDocument)).toBeInTheDocument();
    // 行不移除、列表不刷新（其余行仍在原位）
    expect(screen.getByText('公共制度汇编.pdf')).toBeInTheDocument();
    expect(screen.getByText('行业研报汇总.pdf')).toBeInTheDocument();
    expect(screen.getByText('历史遗留材料.pdf')).toBeInTheDocument();
  });

  it('409 submission_scope_changed：刷新列表（失效行消失，其余行保留）', async () => {
    const token = loginToken('ops-wang');
    await renderApprovals(
      <ApprovalSubmissionsLayer />,
      opsUser(),
      contractAdminApi(token),
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await user.click(within(rowOf('跨部门协作指引.pdf')).getByRole('button', { name: copyManage.approve }));

    await waitFor(() => expect(screen.queryByText('跨部门协作指引.pdf')).not.toBeInTheDocument());
    expect(screen.getByText('行业研报汇总.pdf')).toBeInTheDocument();
    expect(screen.getByText('公共制度汇编.pdf')).toBeInTheDocument();
  });

  it('409 submitter_pending_delete（冻结投稿人种子）：刷新列表（失效行消失）', async () => {
    const token = loginToken('ops-wang');
    await renderApprovals(
      <ApprovalSubmissionsLayer />,
      opsUser(),
      contractAdminApi(token),
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await user.click(within(rowOf('历史遗留材料.pdf')).getByRole('button', { name: copyManage.approve }));

    await waitFor(() => expect(screen.queryByText('历史遗留材料.pdf')).not.toBeInTheDocument());
    expect(screen.getByText('行业研报汇总.pdf')).toBeInTheDocument();
  });

  it('409 version_conflict：行内提示 + 刷新列表', async () => {
    const token = loginToken('ops-wang');
    let conflict = true;
    const adminApi = contractAdminApi(token, {
      approveSubmission: vi.fn((id: string, version: number, key: string) => {
        if (conflict) {
          conflict = false;
          return Promise.reject(
            new ApiError({ status: 409, code: 'version_conflict', message: '', details: {}, requestId: null }),
          );
        }
        return call(() => mockKnowledge.approveSubmission(token, id, version, key));
      }),
    });
    await renderApprovals(
      <ApprovalSubmissionsLayer />,
      opsUser(),
      adminApi,
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await screen.findByText('行业研报汇总.pdf');
    const callsBefore = (adminApi.listApprovalSubmissions as ReturnType<typeof vi.fn>).mock.calls.length;
    await user.click(within(rowOf('行业研报汇总.pdf')).getByRole('button', { name: copyManage.approve }));

    expect(await screen.findByText(copyManage.versionConflict)).toBeInTheDocument();
    await waitFor(() =>
      expect((adminApi.listApprovalSubmissions as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(
        callsBefore,
      ),
    );
  });

  it('驳回对话框：可选单行原因随请求提交，成功淡出 + 轻提示', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    const target = pendingSubmission(token, '行业研报汇总.pdf');
    await renderApprovals(
      <ApprovalSubmissionsLayer />,
      opsUser(),
      adminApi,
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await user.click(within(rowOf('行业研报汇总.pdf')).getByRole('button', { name: copyManage.reject }));
    const dialog = await screen.findByRole('dialog', { name: copyManage.rejectDialogTitle });
    await user.type(within(dialog).getByRole('textbox'), '格式不符');
    await user.click(within(dialog).getByRole('button', { name: copyManage.reject }));

    await waitFor(() =>
      expect(adminApi.rejectSubmission).toHaveBeenCalledWith(
        target.submission_id,
        target.version,
        '格式不符',
        expect.stringMatching(/^idem_/),
      ),
    );
    expect(await screen.findByText(copyManage.rejectedNotice)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('行业研报汇总.pdf')).not.toBeInTheDocument());
  });

  it('投稿驳回框在输入原因后以 Enter 提交', async () => {
    const token = loginToken('ops-wang');
    const adminApi = contractAdminApi(token);
    const target = pendingSubmission(token, '行业研报汇总.pdf');
    await renderApprovals(
      <ApprovalSubmissionsLayer />,
      opsUser(),
      adminApi,
      contractSettingsApi(token),
    );
    const user = userEvent.setup();
    await user.click(within(rowOf('行业研报汇总.pdf')).getByRole('button', { name: copyManage.reject }));
    const dialog = await screen.findByRole('dialog', { name: copyManage.rejectDialogTitle });
    await user.type(within(dialog).getByRole('textbox'), '回车原因');

    await user.keyboard('{Enter}');

    await waitFor(() =>
      expect(adminApi.rejectSubmission).toHaveBeenCalledWith(
        target.submission_id,
        target.version,
        '回车原因',
        expect.stringMatching(/^idem_/),
      ),
    );
  });

  it('查看内容：下载原件而不打开 Blob 预览窗口', async () => {
    const token = loginToken('ops-wang');
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);
    const createSpy = vi.spyOn(URL, 'createObjectURL').mockImplementation(() => 'blob:mock-0');
    const revokeSpy = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const downloads: Array<{ href: string; filename: string }> = [];
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function click(this: HTMLAnchorElement) {
      downloads.push({ href: this.href, filename: this.download });
      });
    try {
      const settingsApi = contractSettingsApi(token, {
        getSubmissionContent: vi.fn(async () => new Blob(['content'], { type: 'application/pdf' })),
      });
      await renderApprovals(
        <ApprovalSubmissionsLayer />,
        opsUser(),
        contractAdminApi(token),
        settingsApi,
      );
      const user = userEvent.setup();
      await user.click(within(rowOf('行业研报汇总.pdf')).getByRole('button', { name: copyManage.viewContent }));

      await waitFor(() =>
        expect(downloads).toEqual([{ href: 'blob:mock-0', filename: '行业研报汇总.pdf' }]),
      );
      expect(openSpy).not.toHaveBeenCalled();
      expect(createSpy).toHaveBeenCalledTimes(1);
      expect(revokeSpy).toHaveBeenCalledWith('blob:mock-0');
    } finally {
      openSpy.mockRestore();
      createSpy.mockRestore();
      revokeSpy.mockRestore();
      clickSpy.mockRestore();
    }
  });

  it('查看内容不可用：保留行内提示且不打开预览窗口', async () => {
    const token = loginToken('ops-wang');
    const win = {
      opener: null as Window | null,
      document: { write: vi.fn(), close: vi.fn() },
      location: { href: '' },
      close: vi.fn(),
      addEventListener: vi.fn(),
    };
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(win as unknown as Window);
    const settingsApi = contractSettingsApi(token, {
      getSubmissionContent: vi.fn(async () => {
        throw new ApiError({
          status: 404,
          code: 'submission_content_unavailable',
          message: '',
          details: {},
          requestId: null,
        });
      }),
    });
    try {
      await renderApprovals(
        <ApprovalSubmissionsLayer />,
        opsUser(),
        contractAdminApi(token),
        settingsApi,
      );
      const user = userEvent.setup();
      await user.click(within(rowOf('行业研报汇总.pdf')).getByRole('button', { name: copyManage.viewContent }));

      expect(await screen.findByText(copyManage.contentUnavailable)).toBeInTheDocument();
      expect(openSpy).not.toHaveBeenCalled();
    } finally {
      openSpy.mockRestore();
    }
  });

  it('admin 展示全部待审投稿并附目标空间筛选（超管端 §7.3）', async () => {
    const token = loginToken('admin');
    const adminApi = contractAdminApi(token);
    await renderApprovals(
      <ApprovalSubmissionsLayer />,
      adminUser(),
      adminApi,
      contractSettingsApi(token),
    );
    // 后端范围：公共库 4 条 + 部门库 3 条
    expect(await screen.findByText('行业研报汇总.pdf')).toBeInTheDocument();
    expect(screen.getByText('招聘流程优化.docx')).toBeInTheDocument();
    expect(screen.getByText('第三季度预算说明.pdf')).toBeInTheDocument();
    // 待审含部门库投稿 → 渲染目标空间筛选分段控件
    const filter = screen.getByRole('radiogroup', { name: copyApprovals.filterSpaceAria });
    expect(within(filter).getByRole('radio', { name: copyApprovals.filterAll })).toHaveAttribute('aria-checked', 'true');
    expect(adminApi.listApprovalSubmissions).toHaveBeenCalledWith();
  });
});
